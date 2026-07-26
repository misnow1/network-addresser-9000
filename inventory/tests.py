"""Tests for the invariants raised in PR #1 review — rack-slot validity,
device-port identity/wiring, and admin-populated audit fields — plus the
port-profile/materialization invariants added in phase 8 (ADR 0010).

These deliberately include direct-ORM writes (``bulk_create``, ``.create()``)
that skip ``full_clean()``, since ``Model.clean()`` is not invoked by
``save()`` — only a DB-level ``CheckConstraint``, or an explicit guard
inside ``save()`` itself, can guard those paths.
"""

import io
import ipaddress

from auditlog.models import LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.forms import inlineformset_factory
from django.test import RequestFactory, TestCase

from .admin import (
    AuditedModelAdminMixin,
    NetworkDeviceAdmin,
    NetworkDevicePortInline,
    NetworkDeviceTypeAdmin,
    NetworkSwitchAdmin,
    NetworkSwitchPortInline,
    NetworkSwitchTypeAdmin,
    RackAdmin,
    VLANAdmin,
)
from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    NetworkSwitchTypePort,
    PortType,
    Rack,
    RackVlanRange,
)
from .suggestions import (
    dhcp_range_overlaps_cidr,
    prefix_length_for_capacity,
    suggest_default_gateway,
    suggest_rack_vlan_range,
    suggest_slot_address,
)

User = get_user_model()


def _make_switch_type(port_count: int = 0, **kwargs) -> NetworkSwitchType:
    """Create a ``NetworkSwitchType`` together with a complete, contiguously
    numbered ``NetworkSwitchTypePort`` profile (ADR 0010).

    ``port_count=0`` (the default) means no ports at all — most tests here
    don't care about materialized ports, they add/inspect switch ports
    manually, so a zero-port profile keeps the old, pre-materialization
    fixture shape working unchanged. Tests of materialization itself pass
    an explicit ``port_count``.
    """
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
    """See ``_make_switch_type`` — same reasoning for
    ``NetworkDeviceType``/``NetworkDeviceTypePort``/``NetworkDevicePort``.
    """
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


class RackSlotAssignmentTests(TestCase):
    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_rack_slot_must_be_at_least_one(self) -> None:
        switch = NetworkSwitch(switch_type=self.switch_type, rack=self.rack, rack_slot=0)
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_rack_and_slot_are_all_or_neither(self) -> None:
        switch = NetworkSwitch(switch_type=self.switch_type, rack=self.rack, rack_slot=None)
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_rack_slot_cannot_exceed_slot_count(self) -> None:
        switch = NetworkSwitch(
            switch_type=self.switch_type, rack=self.rack, rack_slot=self.rack.slot_count + 1
        )
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_switch_and_device_cannot_share_a_slot(self) -> None:
        NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        device = NetworkDevice(device_type=self.device_type, rack=self.rack, rack_slot=1)
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_device_and_switch_cannot_share_a_slot(self) -> None:
        NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1)
        switch = NetworkSwitch(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_db_rejects_zero_rack_slot_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitch.objects.bulk_create(
                [NetworkSwitch(switch_type=self.switch_type, rack=self.rack, rack_slot=0)]
            )

    def test_db_rejects_rack_without_slot_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevice.objects.bulk_create(
                [NetworkDevice(device_type=self.device_type, rack=self.rack, rack_slot=None)]
            )


class NetworkSwitchPortTests(TestCase):
    """Instance-port invariants that still apply after ADR 0010 —
    ``port_number`` is still required and >=1. The profile-level bound
    check ("can't exceed the type's port_count") now lives on
    ``NetworkSwitchTypePort`` instead — see ``NetworkSwitchTypePortTests``.
    """

    def setUp(self) -> None:
        self.switch_type = _make_switch_type()
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type)

    def test_port_number_must_be_at_least_one(self) -> None:
        port = NetworkSwitchPort(switch=self.switch, port_number=0)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_db_rejects_zero_port_number_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitchPort.objects.bulk_create([NetworkSwitchPort(switch=self.switch, port_number=0)])


class NetworkSwitchTypePortTests(TestCase):
    """The "can't exceed the profile's declared port_count" and numbering
    checks that used to live on the instance port (``NetworkSwitchPort``)
    now live here instead — ADR 0010 moved bound-checking to the template.
    """

    def test_port_number_must_be_at_least_one(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        type_port = NetworkSwitchTypePort(switch_type=switch_type, port_number=0, port_type=PortType.GBE_RJ45)
        with self.assertRaises(ValidationError):
            type_port.full_clean()

    def test_port_number_cannot_exceed_switch_type_port_count(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        type_port = NetworkSwitchTypePort(switch_type=switch_type, port_number=3, port_type=PortType.GBE_RJ45)
        with self.assertRaises(ValidationError):
            type_port.full_clean()

    def test_db_rejects_zero_port_number_bypassing_clean(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitchTypePort.objects.bulk_create(
                [NetworkSwitchTypePort(switch_type=switch_type, port_number=0, port_type=PortType.GBE_RJ45)]
            )


class NetworkDeviceTypePortTests(TestCase):
    """Device type ports have no port_number bound (unlike switch type
    ports) since port_number is optional/non-sequential for these — but
    ``description`` (the port's identity) is required and unique per
    profile, and ``ordinal`` is auto-assigned.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Default", port_count=0
        )

    def test_blank_description_rejected_via_clean(self) -> None:
        type_port = NetworkDeviceTypePort(
            device_type=self.device_type, description="", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        with self.assertRaises(ValidationError):
            type_port.full_clean()

    def test_db_rejects_blank_description_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDeviceTypePort.objects.bulk_create(
                [
                    NetworkDeviceTypePort(
                        device_type=self.device_type,
                        description="",
                        port_type=PortType.GBE_RJ45,
                        vlan=self.vlan,
                    )
                ]
            )

    def test_ordinal_auto_assigned_sequentially(self) -> None:
        first = NetworkDeviceTypePort.objects.create(
            device_type=self.device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        second = NetworkDeviceTypePort.objects.create(
            device_type=self.device_type, description="Port B", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        self.assertEqual(first.ordinal, 1)
        self.assertEqual(second.ordinal, 2)

    def test_description_unique_per_device_type(self) -> None:
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDeviceTypePort.objects.create(
                device_type=self.device_type,
                description="Port A",
                port_type=PortType.GBE_RJ45,
                vlan=self.vlan,
            )


class NetworkDevicePortTests(TestCase):
    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = _make_device_type()
        self.device = NetworkDevice.objects.create(device_type=self.device_type)
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        self.switch_port = NetworkSwitchPort.objects.create(switch=switch, port_number=1)

    def test_description_unique_per_device(self) -> None:
        NetworkDevicePort.objects.create(
            device=self.device, description="Port A", vlan=self.vlan, address="10.200.0.10"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=self.device, description="Port A", vlan=self.vlan, address="10.200.0.11"
            )

    def test_switch_property_derives_from_switch_port(self) -> None:
        port = NetworkDevicePort.objects.create(
            device=self.device,
            description="Port A",
            vlan=self.vlan,
            address="10.200.0.10",
            switch_port=self.switch_port,
        )
        self.assertEqual(port.switch, self.switch_port.switch)

    def test_switch_property_is_none_without_switch_port(self) -> None:
        port = NetworkDevicePort.objects.create(
            device=self.device, description="Port A", vlan=self.vlan, address="10.200.0.10"
        )
        self.assertIsNone(port.switch)

    def test_switch_port_can_only_be_claimed_by_one_device_port(self) -> None:
        NetworkDevicePort.objects.create(
            device=self.device,
            description="Port A",
            vlan=self.vlan,
            address="10.200.0.10",
            switch_port=self.switch_port,
        )
        other_device = NetworkDevice.objects.create(device_type=self.device_type)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=other_device,
                description="Port A",
                vlan=self.vlan,
                address="10.200.0.11",
                switch_port=self.switch_port,
            )

    def test_dhcp_port_rejects_static_address_via_clean(self) -> None:
        port = NetworkDevicePort(
            device=self.device, description="Port A", vlan=self.vlan, is_dhcp=True, address="10.200.0.10"
        )
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_static_port_requires_address_via_clean(self) -> None:
        port = NetworkDevicePort(device=self.device, description="Port A", vlan=self.vlan, is_dhcp=False)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_db_rejects_dhcp_with_address_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=self.device, description="Port A", vlan=self.vlan, is_dhcp=True, address="10.200.0.10"
            )

    def test_db_rejects_static_without_address_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=self.device, description="Port A", vlan=self.vlan, is_dhcp=False
            )


class AuditedModelAdminTests(TestCase):
    def setUp(self) -> None:
        self.user = User.objects.create_user(username="editor", password="x")
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.post("/admin/inventory/vlan/add/")
        request.user = self.user
        return request

    def test_created_by_is_not_a_form_field(self) -> None:
        admin = VLANAdmin(VLAN, AdminSite())
        form_class = admin.get_form(self._request())
        self.assertNotIn("created_by", form_class.base_fields)

    def test_save_model_sets_created_by_on_creation(self) -> None:
        admin = VLANAdmin(VLAN, AdminSite())
        obj = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        admin.save_model(self._request(), obj, form=None, change=False)
        self.assertEqual(obj.created_by, self.user)

    def test_save_model_does_not_overwrite_created_by_on_change(self) -> None:
        other_user = User.objects.create_user(username="original", password="x")
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21", created_by=other_user)
        admin = VLANAdmin(VLAN, AdminSite())
        admin.save_model(self._request(), vlan, form=None, change=True)
        self.assertEqual(vlan.created_by, other_user)

    def test_device_admin_registered_with_audit_mixin(self) -> None:
        # Sanity check that the mixin was actually applied where it matters,
        # not just on VLANAdmin.
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        self.assertTrue(hasattr(admin, "save_formset"))


class InlineFormsetSaveTests(TestCase):
    """Regression tests for save_formset: formset.save(commit=False) doesn't
    touch formset.deleted_objects, so a naive rewrite of the stock
    ModelAdmin.save_formset (to populate created_by) can silently break
    inline deletion. Exercised via a real inlineformset_factory formset,
    matching what the admin actually builds and submits.
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="editor", password="x")
        self.factory = RequestFactory()
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.existing_range = RackVlanRange.objects.create(
            rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27"
        )

    def _request(self):
        request = self.factory.post(f"/admin/inventory/rack/{self.rack.pk}/change/")
        request.user = self.user
        return request

    def _formset(self, **extra_data: str):
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            Rack, RackVlanRange, fields=["vlan", "address_range"], extra=0, can_delete=True
        )
        data = {
            "vlan_ranges-TOTAL_FORMS": "1",
            "vlan_ranges-INITIAL_FORMS": "1",
            "vlan_ranges-MIN_NUM_FORMS": "0",
            "vlan_ranges-MAX_NUM_FORMS": "1000",
            "vlan_ranges-0-id": str(self.existing_range.pk),
            "vlan_ranges-0-rack": str(self.rack.pk),
            "vlan_ranges-0-vlan": str(self.vlan.pk),
            "vlan_ranges-0-address_range": self.existing_range.address_range,
            **extra_data,
        }
        return FormSet(data, instance=self.rack, prefix="vlan_ranges")

    def test_save_formset_deletes_rows_marked_for_deletion(self) -> None:
        formset = self._formset(**{"vlan_ranges-0-DELETE": "on"})
        self.assertTrue(formset.is_valid(), formset.errors)
        admin = RackAdmin(Rack, AdminSite())
        admin.save_formset(self._request(), form=None, formset=formset, change=True)
        self.assertFalse(RackVlanRange.objects.filter(pk=self.existing_range.pk).exists())

    def test_save_formset_still_saves_undeleted_rows(self) -> None:
        formset = self._formset(**{"vlan_ranges-0-address_range": "10.200.1.32/27"})
        self.assertTrue(formset.is_valid(), formset.errors)
        admin = RackAdmin(Rack, AdminSite())
        admin.save_formset(self._request(), form=None, formset=formset, change=True)
        self.existing_range.refresh_from_db()
        self.assertEqual(self.existing_range.address_range, "10.200.1.32/27")

    def test_blank_range_suggested_for_inline_on_unsaved_new_rack(self) -> None:
        # Django's admin "Add" view validates inline formsets before the new
        # parent is saved, so form.instance.rack_id is None even though
        # form.instance.rack (the actual object) is set — see
        # BaseInlineFormSet._construct_form. The suggestion must still work
        # off the object, not the (not-yet-real) id.
        unsaved_rack = Rack(name="New Rack", slot_count=4)
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            Rack, RackVlanRange, fields=["vlan", "address_range"], extra=1, can_delete=True
        )
        data = {
            "vlan_ranges-TOTAL_FORMS": "1",
            "vlan_ranges-INITIAL_FORMS": "0",
            "vlan_ranges-MIN_NUM_FORMS": "0",
            "vlan_ranges-MAX_NUM_FORMS": "1000",
            "vlan_ranges-0-vlan": str(self.vlan.pk),
            "vlan_ranges-0-address_range": "",
        }
        formset = FormSet(data, instance=unsaved_rack, prefix="vlan_ranges")
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(formset.forms[0].instance.address_range, "10.200.0.0/29")


class UnsavedParentInlineSuggestionTests(TestCase):
    """Suggestions must work when adding a switch/device and its address
    inline together on one admin "Add" page — not just when editing an
    already-saved parent. See test above for the equivalent RackVlanRange
    case and its explanation of why this is otherwise broken.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_switch_address_suggested_for_inline_on_unsaved_new_switch(self) -> None:
        unsaved_switch = NetworkSwitch(switch_type=self.switch_type, rack=self.rack, rack_slot=2)
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            NetworkSwitch, NetworkSwitchAddress, fields=["vlan", "address"], extra=1
        )
        data = {
            "addresses-TOTAL_FORMS": "1",
            "addresses-INITIAL_FORMS": "0",
            "addresses-MIN_NUM_FORMS": "0",
            "addresses-MAX_NUM_FORMS": "1000",
            "addresses-0-vlan": str(self.vlan.pk),
            "addresses-0-address": "",
        }
        formset = FormSet(data, instance=unsaved_switch, prefix="addresses")
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(formset.forms[0].instance.address, "10.200.1.2")

    def test_device_port_address_suggested_for_inline_on_unsaved_new_device(self) -> None:
        unsaved_device = NetworkDevice(device_type=self.device_type, rack=self.rack, rack_slot=3)
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            NetworkDevice, NetworkDevicePort, fields=["port_number", "vlan", "is_dhcp", "address"], extra=1
        )
        data = {
            "ports-TOTAL_FORMS": "1",
            "ports-INITIAL_FORMS": "0",
            "ports-MIN_NUM_FORMS": "0",
            "ports-MAX_NUM_FORMS": "1000",
            "ports-0-port_number": "1",
            "ports-0-vlan": str(self.vlan.pk),
            "ports-0-address": "",
        }
        formset = FormSet(data, instance=unsaved_device, prefix="ports")
        self.assertTrue(formset.is_valid(), formset.errors)
        self.assertEqual(formset.forms[0].instance.address, "10.200.1.3")


class SuggestionFunctionTests(TestCase):
    """Pure-function tests for inventory.suggestions — no DB involved."""

    def test_suggest_default_gateway_is_lowest_host_address(self) -> None:
        self.assertEqual(suggest_default_gateway("10.200.0.0/21"), "10.200.0.1")

    def test_dhcp_range_overlaps_cidr_when_range_starts_inside_block(self) -> None:
        self.assertTrue(dhcp_range_overlaps_cidr("10.200.0.100", "10.200.1.50", "10.200.0.0/24"))

    def test_dhcp_range_overlaps_cidr_when_block_is_inside_range(self) -> None:
        self.assertTrue(dhcp_range_overlaps_cidr("10.200.0.0", "10.200.7.255", "10.200.3.0/24"))

    def test_dhcp_range_overlaps_cidr_touching_at_block_boundary(self) -> None:
        self.assertTrue(dhcp_range_overlaps_cidr("10.200.0.255", "10.200.1.5", "10.200.1.0/24"))

    def test_dhcp_range_overlaps_cidr_false_when_disjoint(self) -> None:
        self.assertFalse(dhcp_range_overlaps_cidr("10.200.0.1", "10.200.0.254", "10.200.1.0/24"))

    def test_dhcp_range_overlaps_cidr_normalizes_reversed_start_end(self) -> None:
        # start/end ordering is only enforced by VLAN.clean(), not at the DB
        # layer (a string-based CheckConstraint can't express IPv4 ordering
        # correctly) — a reversed pair reaching this function must not
        # silently compute a wrong (false-negative) overlap answer.
        self.assertTrue(dhcp_range_overlaps_cidr("10.200.5.5", "10.200.1.5", "10.200.3.0/24"))

    def test_prefix_length_for_capacity_matches_worked_example(self) -> None:
        # DESIGN.md's worked example: a rack sized for slots 1-30 gets a /27.
        self.assertEqual(prefix_length_for_capacity(30), 27)

    def test_prefix_length_for_capacity_single_slot(self) -> None:
        # 1 slot needs the base address, slot 1, and a reserved top address: 3
        # addresses, rounded up to the next power of two (/30, 4 addresses).
        self.assertEqual(prefix_length_for_capacity(1), 30)

    def test_prefix_length_for_capacity_larger_rack(self) -> None:
        self.assertEqual(prefix_length_for_capacity(62), 26)

    def test_prefix_length_for_capacity_reserves_top_address(self) -> None:
        # A naive "slot_count + 1" rule would give slot_count=3 a /30 (4
        # addresses), putting slot 3 on that block's own top/broadcast-like
        # address. Reserving the top address too pushes it out to a /29.
        self.assertEqual(prefix_length_for_capacity(3), 29)

    def test_suggest_rack_vlan_range_first_block_when_nothing_used(self) -> None:
        self.assertEqual(suggest_rack_vlan_range("10.200.0.0/21", 30, []), "10.200.0.0/27")

    def test_suggest_rack_vlan_range_packs_sequentially_after_used_blocks(self) -> None:
        result = suggest_rack_vlan_range("10.200.0.0/21", 30, ["10.200.0.0/27", "10.200.0.32/27"])
        self.assertEqual(result, "10.200.0.64/27")

    def test_suggest_rack_vlan_range_skips_dhcp_range(self) -> None:
        result = suggest_rack_vlan_range("10.200.0.0/21", 30, [], dhcp_range=("10.200.0.1", "10.200.0.254"))
        self.assertEqual(result, "10.200.1.0/27")

    def test_suggest_rack_vlan_range_none_when_rack_too_big_for_subnet(self) -> None:
        self.assertIsNone(suggest_rack_vlan_range("10.200.1.0/27", 1000, []))

    def test_suggest_rack_vlan_range_none_when_subnet_exhausted(self) -> None:
        used = [str(n) for n in ipaddress.IPv4Network("10.200.0.0/21").subnets(new_prefix=27)]
        self.assertIsNone(suggest_rack_vlan_range("10.200.0.0/21", 30, used))

    def test_suggest_slot_address(self) -> None:
        self.assertEqual(suggest_slot_address("10.200.1.0/27", 1), "10.200.1.1")
        self.assertEqual(suggest_slot_address("10.200.1.0/27", 5), "10.200.1.5")


class VLANSuggestionTests(TestCase):
    def test_blank_gateway_filled_on_create_dhcp_range_stays_blank(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        vlan.full_clean()
        self.assertEqual(vlan.default_gateway, "10.200.0.1")
        self.assertIsNone(vlan.dhcp_range_start)
        self.assertIsNone(vlan.dhcp_range_end)

    def test_explicit_values_are_preserved(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            default_gateway="10.200.0.254",
            dhcp_range_start="10.200.7.1",
            dhcp_range_end="10.200.7.254",
        )
        vlan.full_clean()
        self.assertEqual(vlan.default_gateway, "10.200.0.254")
        self.assertEqual(vlan.dhcp_range_start, "10.200.7.1")
        self.assertEqual(vlan.dhcp_range_end, "10.200.7.254")

    def test_dhcp_range_start_and_end_stay_blank_when_not_provided(self) -> None:
        vlan = VLAN(name="Tiny", vlan_id=201, subnet="10.201.1.0/27")
        vlan.full_clean()
        self.assertIsNone(vlan.dhcp_range_start)
        self.assertIsNone(vlan.dhcp_range_end)

    def test_clearing_on_update_is_not_silently_refilled(self) -> None:
        vlan = VLAN.objects.create(
            name="Control", vlan_id=200, subnet="10.200.0.0/21", default_gateway="10.200.0.1"
        )
        vlan.default_gateway = None
        vlan.full_clean()
        self.assertIsNone(vlan.default_gateway)

    def test_gateway_suggestion_skipped_for_slash_32_subnet(self) -> None:
        vlan = VLAN(name="PointToPoint", vlan_id=202, subnet="10.202.0.1/32")
        vlan.full_clean()  # must not raise ipaddress.AddressValueError
        self.assertIsNone(vlan.default_gateway)

    def test_gateway_outside_subnet_raises(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", default_gateway="10.201.0.1")
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_outside_subnet_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.201.0.1",
            dhcp_range_end="10.201.0.254",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_start_only_raises(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", dhcp_range_start="10.200.0.10")
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_end_only_raises(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", dhcp_range_end="10.200.0.200")
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_start_after_end_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.0.200",
            dhcp_range_end="10.200.0.10",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_start_equal_to_end_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.0.10",
            dhcp_range_end="10.200.0.10",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_containing_network_address_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.0.0",
            dhcp_range_end="10.200.0.50",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_containing_broadcast_address_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.7.200",
            dhcp_range_end="10.200.7.255",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_dhcp_range_containing_default_gateway_raises(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            default_gateway="10.200.0.1",
            dhcp_range_start="10.200.0.1",
            dhcp_range_end="10.200.0.50",
        )
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_subnet_to_exclude_existing_rack_range_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=30)
        RackVlanRange.objects.create(rack=rack, vlan=vlan, address_range="10.200.1.0/27")
        vlan.subnet = "10.205.0.0/21"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_dhcp_range_to_overlap_existing_rack_range_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=30)
        RackVlanRange.objects.create(rack=rack, vlan=vlan, address_range="10.200.1.0/27")
        vlan.dhcp_range_start = "10.200.1.10"
        vlan.dhcp_range_end = "10.200.1.20"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_dhcp_range_to_newly_include_existing_switch_address_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=rack, rack_slot=1)
        # Direct-ORM create bypasses NetworkSwitchAddress.clean()'s own
        # DHCP-range check (see this module's docstring) — needed here since
        # the DHCP range that will swallow this address isn't set yet.
        NetworkSwitchAddress.objects.create(switch=switch, vlan=vlan, address="10.200.5.1")
        vlan.dhcp_range_start = "10.200.5.0"
        vlan.dhcp_range_end = "10.200.5.10"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_dhcp_range_to_newly_include_existing_device_port_address_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type)
        NetworkDevicePort.objects.create(device=device, description="Port A", vlan=vlan, address="10.200.5.2")
        vlan.dhcp_range_start = "10.200.5.0"
        vlan.dhcp_range_end = "10.200.5.10"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_new_switch_address_inside_dhcp_range_raises(self) -> None:
        vlan = VLAN.objects.create(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.5.0",
            dhcp_range_end="10.200.5.10",
        )
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=rack, rack_slot=1)
        RackVlanRange.objects.create(rack=rack, vlan=vlan, address_range="10.200.5.0/27")
        address = NetworkSwitchAddress(switch=switch, vlan=vlan, address="10.200.5.5")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_new_device_port_address_inside_dhcp_range_raises(self) -> None:
        vlan = VLAN.objects.create(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.5.0",
            dhcp_range_end="10.200.5.10",
        )
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type, rack=rack, rack_slot=1)
        RackVlanRange.objects.create(rack=rack, vlan=vlan, address_range="10.200.5.0/27")
        port = NetworkDevicePort(device=device, description="Port A", vlan=vlan, address="10.200.5.5")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_db_rejects_dhcp_range_start_only_bypassing_clean(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", dhcp_range_start="10.200.0.10")
        with self.assertRaises(IntegrityError), transaction.atomic():
            VLAN.objects.bulk_create([vlan])

    def test_db_rejects_dhcp_range_end_only_bypassing_clean(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", dhcp_range_end="10.200.0.200")
        with self.assertRaises(IntegrityError), transaction.atomic():
            VLAN.objects.bulk_create([vlan])

    def test_malformed_dhcp_range_end_with_existing_dependents_raises_validation_not_crash(self) -> None:
        # full_clean() still calls clean() even after clean_fields() has
        # already flagged dhcp_range_end as malformed — clean() must not
        # leave dhcp_start set while dhcp_end stays None, which would crash
        # the switch_addresses re-check below with a raw TypeError instead
        # of the ValidationError clean_fields() already produced.
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=vlan, address="10.200.5.1")
        vlan.dhcp_range_start = "10.200.0.10"
        vlan.dhcp_range_end = "not-an-ip"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_subnet_to_exclude_existing_switch_address_raises(self) -> None:
        # A static address is allowed even without a RackVlanRange (it only
        # has to fit the VLAN's subnet in that case), so a subnet edit has
        # to be checked against it directly, not just against rack ranges.
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=vlan, address="10.200.5.1")
        vlan.subnet = "10.205.0.0/21"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_subnet_to_exclude_existing_device_port_address_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type)
        NetworkDevicePort.objects.create(device=device, description="Port A", vlan=vlan, address="10.200.5.2")
        vlan.subnet = "10.205.0.0/21"
        with self.assertRaises(ValidationError):
            vlan.full_clean()


class RackVlanRangeSuggestionTests(TestCase):
    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=30)

    def test_blank_range_is_suggested_on_create(self) -> None:
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan)
        range_.full_clean()
        self.assertEqual(range_.address_range, "10.200.0.0/27")

    def test_second_rack_gets_next_free_block(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        other_rack = Rack.objects.create(name="Rack 2", slot_count=30)
        range_ = RackVlanRange(rack=other_rack, vlan=self.vlan)
        range_.full_clean()
        self.assertEqual(range_.address_range, "10.200.0.32/27")

    def test_suggestion_skips_vlans_dhcp_range(self) -> None:
        self.vlan.dhcp_range_start = "10.200.0.1"
        self.vlan.dhcp_range_end = "10.200.0.254"
        self.vlan.save()
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan)
        range_.full_clean()
        self.assertEqual(range_.address_range, "10.200.1.0/27")

    def test_explicit_overlap_with_sibling_range_raises(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        other_rack = Rack.objects.create(name="Rack 2", slot_count=30)
        range_ = RackVlanRange(rack=other_rack, vlan=self.vlan, address_range="10.200.0.16/28")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_explicit_overlap_with_dhcp_range_raises(self) -> None:
        self.vlan.dhcp_range_start = "10.200.0.1"
        self.vlan.dhcp_range_end = "10.200.0.254"
        self.vlan.save()
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_range_outside_vlan_subnet_raises(self) -> None:
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan, address_range="10.201.0.0/27")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_blank_range_raises_when_no_suggestion_possible(self) -> None:
        tiny_vlan = VLAN.objects.create(name="Tiny", vlan_id=201, subnet="10.201.1.0/27")
        huge_rack = Rack.objects.create(name="Huge Rack", slot_count=1000)
        range_ = RackVlanRange(rack=huge_rack, vlan=tiny_vlan)
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_explicit_range_too_small_for_rack_slot_count_raises(self) -> None:
        # A /30 has 4 addresses (0-3); a 4-slot rack needs slots 1-4, i.e.
        # 5 addresses (base + slot N), so slot 4 would fall outside it.
        four_slot_rack = Rack.objects.create(name="Rack 2", slot_count=4)
        range_ = RackVlanRange(rack=four_slot_rack, vlan=self.vlan, address_range="10.200.0.0/30")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_editing_range_to_exclude_existing_switch_address_raises(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=self.rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.0.1")
        range_ = RackVlanRange.objects.get(rack=self.rack, vlan=self.vlan)
        range_.address_range = "10.200.0.32/27"
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_editing_range_to_exclude_existing_device_port_address_raises(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, address="10.200.0.2"
        )
        range_ = RackVlanRange.objects.get(rack=self.rack, vlan=self.vlan)
        range_.address_range = "10.200.0.32/27"
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_malformed_address_range_raises_validation_error_not_crash(self) -> None:
        # clean() runs even after clean_fields() has already flagged a bad
        # value, so _validate_range() must not blindly hand a malformed
        # address_range to ipaddress.IPv4Network(strict=True) — that would
        # raise a raw ValueError (-> uncaught 500) instead of ValidationError.
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan, address_range="not-a-cidr")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_suggestion_skips_malformed_vlan_dhcp_range_without_crashing(self) -> None:
        # A malformed persisted dhcp_range_start/end (only reachable via a
        # clean()-bypassing write, e.g. bulk_create/QuerySet.update()) must
        # be skipped like any other malformed sibling value, not handed
        # straight to ipaddress.IPv4Address() unguarded — that would raise a
        # raw ValueError instead of gracefully falling back to "no DHCP
        # range" for suggestion purposes.
        VLAN.objects.filter(pk=self.vlan.pk).update(
            dhcp_range_start="not-an-ip", dhcp_range_end="10.200.0.50"
        )
        self.vlan.refresh_from_db()
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan)
        range_.full_clean()  # must not raise
        self.assertEqual(range_.address_range, "10.200.0.0/27")

    def test_explicit_range_with_malformed_vlan_dhcp_range_does_not_crash(self) -> None:
        # Same guard, but for _validate_range()'s own overlap check (an
        # explicit, non-blank address_range skips the suggestion path
        # entirely, so this exercises a different code path than the test
        # above).
        VLAN.objects.filter(pk=self.vlan.pk).update(
            dhcp_range_start="not-an-ip", dhcp_range_end="10.200.0.50"
        )
        self.vlan.refresh_from_db()
        range_ = RackVlanRange(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        range_.full_clean()  # must not raise


class RackSlotCountEditTests(TestCase):
    """Editing Rack.slot_count must be re-validated against what already
    depends on it: existing RackVlanRanges (raised too small) and already
    -assigned equipment (rack_slot beyond the new, lower count).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_increasing_slot_count_beyond_existing_range_capacity_raises(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        # 10.200.1.0/29 has 8 addresses: room for a 4-slot rack (needs 6) but
        # not a 10-slot one (needs 12).
        RackVlanRange.objects.create(rack=rack, vlan=self.vlan, address_range="10.200.1.0/29")
        rack.slot_count = 10
        with self.assertRaises(ValidationError):
            rack.full_clean()

    def test_increasing_slot_count_within_existing_range_capacity_is_fine(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=rack, vlan=self.vlan, address_range="10.200.1.0/27")
        rack.slot_count = 6
        rack.full_clean()  # must not raise

    def test_decreasing_slot_count_below_assigned_switch_raises(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        NetworkSwitch.objects.create(switch_type=self.switch_type, rack=rack, rack_slot=4)
        rack.slot_count = 2
        with self.assertRaises(ValidationError):
            rack.full_clean()

    def test_decreasing_slot_count_below_assigned_device_raises(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        NetworkDevice.objects.create(device_type=self.device_type, rack=rack, rack_slot=4)
        rack.slot_count = 2
        with self.assertRaises(ValidationError):
            rack.full_clean()


class EquipmentMoveRevalidationTests(TestCase):
    """Clearing or changing a switch/device's rack/rack_slot doesn't run the
    address row's own clean() (it isn't part of this save), so it has to be
    re-checked from the equipment side instead.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack_a = Rack.objects.create(name="Rack A", slot_count=4)
        self.rack_b = Rack.objects.create(name="Rack B", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack_a, vlan=self.vlan, address_range="10.200.1.0/27")
        RackVlanRange.objects.create(rack=self.rack_b, vlan=self.vlan, address_range="10.200.2.0/27")
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_unracking_switch_with_existing_address_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack_a, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        switch.rack = None
        switch.rack_slot = None
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_moving_switch_to_another_racks_range_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack_a, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        switch.rack = self.rack_b
        switch.rack_slot = 1
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_unracking_device_with_existing_static_port_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack_a, rack_slot=2)
        NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, address="10.200.1.2"
        )
        device.rack = None
        device.rack_slot = None
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_moving_device_to_another_racks_range_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack_a, rack_slot=2)
        NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, address="10.200.1.2"
        )
        device.rack = self.rack_b
        device.rack_slot = 2
        with self.assertRaises(ValidationError):
            device.full_clean()


class RackSlotAddressSuggestionTests(TestCase):
    """Suggestion behavior shared by NetworkSwitchAddress and NetworkDevicePort."""

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_switch_address_suggested_when_racked(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan)
        address.full_clean()
        self.assertEqual(address.address, "10.200.1.1")

    def test_switch_address_requires_manual_entry_when_unracked(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan)
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_device_port_address_suggested_when_racked(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, description="Port A", vlan=self.vlan)
        port.full_clean()
        self.assertEqual(port.address, "10.200.1.2")

    def test_device_port_address_requires_manual_entry_without_rack_range(self) -> None:
        other_vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, description="Port A", vlan=other_vlan)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_switch_address_manually_entered_without_rack_range_still_raises(self) -> None:
        # Racked equipment always requires an assigned RackVlanRange, even
        # for a manually-typed address within the VLAN's subnet — otherwise
        # it could land inside the DHCP range or on the gateway.
        other_vlan = VLAN.objects.create(
            name="Dante Primary",
            vlan_id=201,
            subnet="10.201.0.0/21",
            dhcp_range_start="10.201.0.1",
            dhcp_range_end="10.201.0.254",
        )
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        address = NetworkSwitchAddress(switch=switch, vlan=other_vlan, address="10.201.0.5")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_device_port_address_manually_entered_without_rack_range_still_raises(self) -> None:
        other_vlan = VLAN.objects.create(
            name="Dante Primary",
            vlan_id=201,
            subnet="10.201.0.0/21",
            dhcp_range_start="10.201.0.1",
            dhcp_range_end="10.201.0.254",
        )
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, description="Port A", vlan=other_vlan, address="10.201.0.5")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_unracked_switch_static_address_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan, address="10.200.1.5")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_unracked_device_static_port_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type)
        port = NetworkDevicePort(device=device, description="Port A", vlan=self.vlan, address="10.200.1.5")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_switch_address_outside_vlan_subnet_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan, address="10.201.0.1")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_switch_address_outside_rack_range_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        # Within the VLAN's /21 subnet, but outside the rack's 10.200.1.0/27 range.
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan, address="10.200.2.1")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_device_port_outside_rack_range_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, description="Port A", vlan=self.vlan, address="10.200.2.2")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_switch_addresses_cannot_collide_on_same_vlan(self) -> None:
        switch_a = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        switch_b = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=2)
        NetworkSwitchAddress.objects.create(switch=switch_a, vlan=self.vlan, address="10.200.1.1")
        conflicting = NetworkSwitchAddress(switch=switch_b, vlan=self.vlan, address="10.200.1.1")
        with self.assertRaises(ValidationError):
            conflicting.full_clean()

    def test_device_port_address_cannot_collide_with_switch_address_on_same_vlan(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        conflicting = NetworkDevicePort(
            device=device, description="Port A", vlan=self.vlan, address="10.200.1.1"
        )
        with self.assertRaises(ValidationError):
            conflicting.full_clean()


class RemovalSemanticsTests(TestCase):
    """Locks in ADR 0007: containers block removal while non-empty; leaf
    references (a switch a device is plugged into) unassign rather than
    cascade-delete. These invariants come from the on_delete choices made
    in the schema itself, not from clean()/full_clean() — so exercised via
    plain .delete() calls rather than full_clean().
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_rack_removal_blocked_while_switch_assigned(self) -> None:
        NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        with self.assertRaises(ProtectedError):
            self.rack.delete()

    def test_rack_removal_blocked_while_device_assigned(self) -> None:
        NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1)
        with self.assertRaises(ProtectedError):
            self.rack.delete()

    def test_rack_removal_succeeds_once_empty(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.rack.delete()
        self.assertFalse(Rack.objects.filter(pk=self.rack.pk).exists())

    def test_vlan_removal_blocked_by_rack_vlan_range(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_by_switch_address(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_by_device_port(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type)
        NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, address="10.200.1.2"
        )
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_switch_type_removal_blocked_while_switch_exists(self) -> None:
        NetworkSwitch.objects.create(switch_type=self.switch_type)
        with self.assertRaises(ProtectedError):
            self.switch_type.delete()

    def test_device_type_removal_blocked_while_device_exists(self) -> None:
        NetworkDevice.objects.create(device_type=self.device_type)
        with self.assertRaises(ProtectedError):
            self.device_type.delete()

    def test_deleting_switch_unassigns_rather_than_deletes_connected_device_port(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        switch_port = NetworkSwitchPort.objects.create(switch=switch, port_number=1)
        device = NetworkDevice.objects.create(device_type=self.device_type)
        device_port = NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, address="10.200.1.2", switch_port=switch_port
        )
        switch.delete()
        device_port.refresh_from_db()
        self.assertIsNone(device_port.switch_port)
        self.assertTrue(NetworkDevice.objects.filter(pk=device.pk).exists())


class SyncRolesCommandTests(TestCase):
    """Locks in the Viewer/Editor/Admin permission sets from CONTEXT.md's
    Roles section, and that the command is idempotent (see ``sync_roles``'s
    docstring for why this can't be a data migration instead).
    """

    def test_viewer_can_only_view(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        viewer = Group.objects.get(name="Viewer")
        codenames = set(viewer.permissions.values_list("codename", flat=True))
        self.assertIn("view_vlan", codenames)
        self.assertIn("view_logentry", codenames)
        self.assertNotIn("add_vlan", codenames)
        self.assertNotIn("change_vlan", codenames)
        self.assertNotIn("delete_vlan", codenames)

    def test_editor_can_view_and_add_but_not_remove(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        editor = Group.objects.get(name="Editor")
        codenames = set(editor.permissions.values_list("codename", flat=True))
        self.assertIn("view_vlan", codenames)
        self.assertIn("add_vlan", codenames)
        self.assertIn("change_vlan", codenames)
        self.assertNotIn("delete_vlan", codenames)

    def test_admin_can_view_add_and_remove(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        admin_group = Group.objects.get(name="Admin")
        codenames = set(admin_group.permissions.values_list("codename", flat=True))
        self.assertIn("view_vlan", codenames)
        self.assertIn("add_vlan", codenames)
        self.assertIn("change_vlan", codenames)
        self.assertIn("delete_vlan", codenames)

    def test_idempotent(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        first_count = Group.objects.get(name="Admin").permissions.count()
        call_command("sync_roles", stdout=io.StringIO())
        self.assertEqual(Group.objects.count(), 3)
        self.assertEqual(Group.objects.get(name="Admin").permissions.count(), first_count)


class RBACAdminPermissionTests(TestCase):
    """Exercises the actual admin views through the test client, proving the
    three roles are enforced end-to-end rather than just by permission-set
    membership (``SyncRolesCommandTests`` above).
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

        self.viewer = User.objects.create_user("viewer", password="testpass123", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Viewer"))

        self.editor = User.objects.create_user("editor", password="testpass123", is_staff=True)
        self.editor.groups.add(Group.objects.get(name="Editor"))

        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))

    def test_viewer_can_view_but_not_add_or_delete(self) -> None:
        self.client.login(username="viewer", password="testpass123")
        self.assertEqual(self.client.get("/admin/inventory/vlan/").status_code, 200)
        self.assertEqual(self.client.get("/admin/inventory/vlan/add/").status_code, 403)
        self.assertEqual(self.client.get(f"/admin/inventory/vlan/{self.vlan.pk}/delete/").status_code, 403)

    def test_editor_can_add_but_not_delete(self) -> None:
        self.client.login(username="editor", password="testpass123")
        self.assertEqual(self.client.get("/admin/inventory/vlan/add/").status_code, 200)
        self.assertEqual(self.client.get(f"/admin/inventory/vlan/{self.vlan.pk}/delete/").status_code, 403)

    def test_admin_can_delete(self) -> None:
        self.client.login(username="adminrole", password="testpass123")
        self.assertEqual(self.client.get(f"/admin/inventory/vlan/{self.vlan.pk}/delete/").status_code, 200)

    def test_switch_bulk_delete_action_hidden_from_viewer_and_editor(self) -> None:
        """The custom ``delete_selected`` shadow (``inventory/admin.py``)
        must carry the same ``permissions=["delete"]`` metadata as the
        built-in action it replaces — omitting it made the action visible
        and invocable by Viewers/Editors too (caught by Codex review).
        """
        switch_admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        factory = RequestFactory()

        for username in ["viewer", "editor"]:
            request = factory.get("/admin/inventory/networkswitch/")
            request.user = getattr(self, username)
            self.assertNotIn("delete_selected", switch_admin.get_actions(request))

        admin_request = factory.get("/admin/inventory/networkswitch/")
        admin_request.user = self.admin_user
        self.assertIn("delete_selected", switch_admin.get_actions(admin_request))


class AuditTrailScopingTests(TestCase):
    """Locks in ADR 0004/0008's scoping: only address overrides, rack/slot
    reassignment, and removals are tracked — not every field on every
    object (a hostname rename, or a port description typo fix).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()
        self.switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="sw1"
        )

    def test_rack_slot_reassignment_is_logged(self) -> None:
        LogEntry.objects.filter(object_pk=str(self.switch.pk)).delete()
        self.switch.rack_slot = 2
        self.switch.save()
        entries = LogEntry.objects.filter(object_pk=str(self.switch.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entries.count(), 1)
        self.assertIn("rack_slot", entries.first().changes_dict)

    def test_hostname_only_edit_is_not_logged(self) -> None:
        LogEntry.objects.filter(object_pk=str(self.switch.pk)).delete()
        self.switch.hostname = "sw1-renamed"
        self.switch.save()
        self.assertFalse(
            LogEntry.objects.filter(object_pk=str(self.switch.pk), action=LogEntry.Action.UPDATE).exists()
        )

    def test_delete_is_logged_with_identifying_object_repr(self) -> None:
        pk = self.switch.pk
        self.switch.delete()
        entry = LogEntry.objects.get(object_pk=str(pk), action=LogEntry.Action.DELETE)
        self.assertIn("sw1", entry.object_repr)

    def test_switch_port_description_edit_is_not_logged(self) -> None:
        port = NetworkSwitchPort.objects.create(switch=self.switch, port_number=1, description="uplink")
        LogEntry.objects.filter(object_pk=str(port.pk)).delete()
        port.description = "typo fix"
        port.save()
        self.assertFalse(
            LogEntry.objects.filter(object_pk=str(port.pk), action=LogEntry.Action.UPDATE).exists()
        )

    def test_unracked_switch_removal_is_still_logged(self) -> None:
        """A spare-pool switch has ``rack``/``rack_slot`` both null — the
        very fields ``include_fields`` scopes edits to — so without
        ``created_at`` also included, auditlog's delete diff would come back
        empty and the removal would go unlogged (caught by Codex review).
        """
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, hostname="spare1")
        pk = switch.pk
        switch.delete()
        self.assertTrue(LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.DELETE).exists())

    def test_dhcp_device_port_removal_is_still_logged(self) -> None:
        """A DHCP port has ``address`` null — same empty-diff gap as the
        unracked-switch case above, but for ``NetworkDevicePort``.
        """
        device = NetworkDevice.objects.create(device_type=self.device_type)
        port = NetworkDevicePort.objects.create(
            device=device, description="Port A", vlan=self.vlan, is_dhcp=True
        )
        pk = port.pk
        port.delete()
        self.assertTrue(LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.DELETE).exists())

    def test_allowed_vlans_change_is_logged(self) -> None:
        """``allowed_vlans`` is a ManyToManyField, which auditlog never diffs
        as an ordinary field — it needs the explicit ``m2m_fields``
        registration (caught by Codex review) to be tracked at all. Still
        holds true now that the field goes through an explicit
        ``NetworkSwitchPortAllowedVlan`` through model (ADR 0010) instead
        of a plain auto-generated join table — ``.add()`` still fires the
        same ``m2m_changed`` signal.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        port = NetworkSwitchPort.objects.create(switch=self.switch, port_number=1)
        LogEntry.objects.filter(object_pk=str(port.pk)).delete()

        port.allowed_vlans.add(self.vlan, other_vlan)

        entry = LogEntry.objects.get(object_pk=str(port.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entry.changes_dict["allowed_vlans"]["operation"], "add")


class TypePortAuditTests(TestCase):
    """New for ADR 0010: type-port templates must meet the same "removals
    (and, here, edits) are always logged" bar as everything else — see
    review finding #10 on the port-profiles plan.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_switch_type_port_creation_is_logged(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=0
        )
        type_port = NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(type_port.pk), action=LogEntry.Action.CREATE).exists()
        )

    def test_switch_type_port_allowed_vlans_change_is_logged(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=0
        )
        type_port = NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        LogEntry.objects.filter(object_pk=str(type_port.pk)).delete()
        type_port.allowed_vlans.add(self.vlan)
        entry = LogEntry.objects.get(object_pk=str(type_port.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entry.changes_dict["allowed_vlans"]["operation"], "add")

    def test_device_type_port_creation_is_logged(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Default", port_count=0
        )
        type_port = NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante Primary", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(type_port.pk), action=LogEntry.Action.CREATE).exists()
        )


class DeleteConfirmationTests(TestCase):
    """The "big scary" removal confirmation (ROADMAP.md phase 4, DESIGN.md's
    Removal semantics) — a generic warning on every delete, plus specific
    callout when deleting a switch would unassign (not delete, per ADR 0007)
    a connected device port.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")

        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_generic_warning_banner_renders_for_plain_model(self) -> None:
        response = self.client.get(f"/admin/inventory/rack/{self.rack.pk}/delete/")
        self.assertContains(response, "permanent and cannot be undone")

    def test_switch_delete_confirmation_lists_connected_device_port(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="sw1"
        )
        switch_port = NetworkSwitchPort.objects.create(switch=switch, port_number=1)
        device = NetworkDevice.objects.create(
            device_type=self.device_type, hostname="dev1", rack=self.rack, rack_slot=2
        )
        NetworkDevicePort.objects.create(
            device=device,
            description="Port A",
            vlan=self.vlan,
            address="10.200.1.2",
            switch_port=switch_port,
        )

        response = self.client.get(f"/admin/inventory/networkswitch/{switch.pk}/delete/")
        self.assertContains(response, "permanent and cannot be undone")
        self.assertContains(response, "dev1")

    def test_switch_delete_confirmation_omits_warning_without_connected_ports(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="sw1"
        )
        response = self.client.get(f"/admin/inventory/networkswitch/{switch.pk}/delete/")
        self.assertNotContains(response, "routes its traffic through it")

    def test_bulk_delete_selected_also_lists_connected_device_port(self) -> None:
        """The single-object ``delete_view`` warning (above) is easy to
        bypass via the changelist's bulk "Delete selected" action — caught
        by Codex review — so ``NetworkSwitchAdmin`` shadows that action too
        (see ``delete_selected`` in ``inventory/admin.py``).
        """
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="sw1"
        )
        switch_port = NetworkSwitchPort.objects.create(switch=switch, port_number=1)
        device = NetworkDevice.objects.create(
            device_type=self.device_type, hostname="dev1", rack=self.rack, rack_slot=2
        )
        NetworkDevicePort.objects.create(
            device=device,
            description="Port A",
            vlan=self.vlan,
            address="10.200.1.2",
            switch_port=switch_port,
        )

        response = self.client.post(
            "/admin/inventory/networkswitch/",
            {"action": "delete_selected", "_selected_action": [str(switch.pk)]},
        )
        self.assertContains(response, "permanent and cannot be undone")
        self.assertContains(response, "dev1")


class TypeProfileNameTests(TestCase):
    """ADR 0010: a Type's identity is (manufacturer, model, name), not just
    (manufacturer, model) — multiple purpose-profiles for one hardware
    model is the whole point.
    """

    def test_switch_type_unique_per_manufacturer_model_name(self) -> None:
        NetworkSwitchType.objects.create(manufacturer="Cisco", model="SG300", name="Default", port_count=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitchType.objects.create(
                manufacturer="Cisco", model="SG300", name="Default", port_count=0
            )

    def test_switch_type_allows_multiple_profiles_for_same_model(self) -> None:
        NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG350", name="For Drive Rack", port_count=0
        )
        NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG350", name="For Amp Rack", port_count=0
        )  # must not raise

    def test_switch_type_name_cannot_be_blank(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitchType.objects.bulk_create(
                [NetworkSwitchType(manufacturer="Cisco", model="SG300", name="", port_count=0)]
            )

    def test_device_type_allows_multiple_profiles_for_same_model(self) -> None:
        NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="with Dante Card", port_count=0
        )
        NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="without Dante Card", port_count=0
        )  # must not raise

    def test_device_type_name_cannot_be_blank(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDeviceType.objects.bulk_create(
                [NetworkDeviceType(manufacturer="Martin Audio", model="IK-42", name="", port_count=0)]
            )


class PortProfileMaterializationTests(TestCase):
    """ADR 0010: a switch/device's ports are copied exactly once from its
    type's *TypePort templates when it's first created.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")

    def test_switch_materializes_ports_from_type(self) -> None:
        switch_type = _make_switch_type(port_count=2)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        ports = list(switch.ports.order_by("port_number"))
        self.assertEqual([p.port_number for p in ports], [1, 2])
        self.assertEqual(ports[0].port_type, PortType.GBE_RJ45)
        self.assertEqual(ports[0].source_type_port, switch_type.type_ports.get(port_number=1))

    def test_device_materializes_ports_as_dhcp(self) -> None:
        device_type = _make_device_type(port_count=2, vlan=self.vlan_a)
        device = NetworkDevice.objects.create(device_type=device_type)
        ports = list(device.ports.order_by("ordinal"))
        self.assertEqual(len(ports), 2)
        for port in ports:
            self.assertTrue(port.is_dhcp)
            self.assertIsNone(port.address)
        self.assertEqual({p.description for p in ports}, {"Port 1", "Port 2"})

    def test_materialized_port_created_by_matches_parent(self) -> None:
        user = User.objects.create_user(username="creator", password="x")
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch(switch_type=switch_type, created_by=user)
        switch.save()
        self.assertEqual(switch.ports.get().created_by, user)

    def test_incomplete_switch_profile_rejected(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        with self.assertRaises(ValidationError):
            NetworkSwitch.objects.create(switch_type=switch_type)

    def test_noncontiguous_switch_numbering_rejected(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=3, port_type=PortType.GBE_RJ45
        )
        with self.assertRaises(ValidationError):
            NetworkSwitch.objects.create(switch_type=switch_type)

    def test_incomplete_device_profile_rejected(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Default", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Port 1", port_type=PortType.GBE_RJ45, vlan=self.vlan_a
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type)

    def test_resaving_switch_does_not_duplicate_ports(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        switch.hostname = "renamed"
        switch.save()
        self.assertEqual(switch.ports.count(), 1)

    def test_switch_allowed_vlans_copied_from_type_port(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        type_port = switch_type.type_ports.get()
        type_port.allowed_vlans.set([self.vlan_a, self.vlan_b])
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        self.assertEqual(set(port.allowed_vlans.all()), {self.vlan_a, self.vlan_b})


class PortProfileAtomicityTests(TestCase):
    """Review finding #3: the transaction must wrap the parent save *and*
    materialization together, so a failed profile leaves neither the
    switch/device nor any partial ports behind.
    """

    def test_failed_switch_materialization_rolls_back_parent(self) -> None:
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=2
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        # port_count=2 but only 1 type port defined -> materialization fails.
        with self.assertRaises(ValidationError):
            NetworkSwitch.objects.create(switch_type=switch_type)
        self.assertFalse(NetworkSwitch.objects.filter(switch_type=switch_type).exists())
        self.assertFalse(NetworkSwitchPort.objects.filter(switch__switch_type=switch_type).exists())

    def test_failed_device_materialization_rolls_back_parent(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Default", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Port 1", port_type=PortType.GBE_RJ45, vlan=vlan
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type)
        self.assertFalse(NetworkDevice.objects.filter(device_type=device_type).exists())
        self.assertFalse(NetworkDevicePort.objects.filter(device__device_type=device_type).exists())


class PortProfileTypeImmutabilityTests(TestCase):
    """ADR 0010: an instance's type is fixed at creation — re-typing means
    removing and recreating it, not editing the field.
    """

    def setUp(self) -> None:
        self.switch_type_a = _make_switch_type()
        self.switch_type_b = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Other Profile", port_count=0
        )
        self.device_type_a = _make_device_type()
        self.device_type_b = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Other Profile", port_count=0
        )

    def test_switch_type_cannot_change_via_plain_save(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type_a)
        switch.switch_type = self.switch_type_b
        with self.assertRaises(ValidationError):
            switch.save()  # no full_clean() — proves the guard lives in save(), not just clean()

    def test_switch_type_cannot_change_via_full_clean(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type_a)
        switch.switch_type = self.switch_type_b
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_device_type_cannot_change_via_plain_save(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type_a)
        device.device_type = self.device_type_b
        with self.assertRaises(ValidationError):
            device.save()

    def test_save_update_fields_excluding_switch_type_is_unaffected(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type_a, hostname="sw1")
        switch.switch_type = self.switch_type_b  # in-memory only
        switch.hostname = "sw1-renamed"
        switch.save(update_fields=["hostname"])  # doesn't touch switch_type -> must not raise
        switch.refresh_from_db()
        self.assertEqual(switch.switch_type, self.switch_type_a)
        self.assertEqual(switch.hostname, "sw1-renamed")

    def test_switch_type_readonly_in_admin_change_view(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type_a)
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        self.assertIn("switch_type", admin.get_readonly_fields(RequestFactory().get("/"), switch))

    def test_switch_type_editable_in_admin_add_view(self) -> None:
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        self.assertNotIn("switch_type", admin.get_readonly_fields(RequestFactory().get("/"), None))

    def test_device_type_readonly_in_admin_change_view(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type_a)
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        self.assertIn("device_type", admin.get_readonly_fields(RequestFactory().get("/"), device))


class PortProfileLockedFieldTests(TestCase):
    """ADR 0010: locked instance-port fields must be enforced in save()
    itself, not just clean() — proven here via plain .save() with no
    full_clean() call.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        self.switch_type = _make_switch_type(port_count=1)
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        self.switch_port = self.switch.ports.get()
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan_a)
        self.device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1)
        self.device_port = self.device.ports.get()

    def test_switch_port_type_cannot_change_via_plain_save(self) -> None:
        self.switch_port.port_type = PortType.TEN_GBE_SFP_PLUS
        with self.assertRaises(ValidationError):
            self.switch_port.save()

    def test_switch_port_vlan_purpose_remains_editable(self) -> None:
        self.switch_port.native_vlan = self.vlan_a
        self.switch_port.description = "uplink"
        self.switch_port.save()  # must not raise
        self.switch_port.refresh_from_db()
        self.assertEqual(self.switch_port.native_vlan, self.vlan_a)

    def test_device_port_description_cannot_change_via_plain_save(self) -> None:
        self.device_port.description = "Renamed"
        with self.assertRaises(ValidationError):
            self.device_port.save()

    def test_device_port_vlan_cannot_change_via_plain_save(self) -> None:
        self.device_port.vlan = self.vlan_b
        with self.assertRaises(ValidationError):
            self.device_port.save()

    def test_device_port_type_cannot_change_via_plain_save(self) -> None:
        self.device_port.port_type = PortType.TEN_GBE_SFP_PLUS
        with self.assertRaises(ValidationError):
            self.device_port.save()

    def test_device_port_can_be_made_static(self) -> None:
        self.device_port.is_dhcp = False
        self.device_port.address = "10.200.1.1"
        self.device_port.save()  # must not raise
        self.device_port.refresh_from_db()
        self.assertEqual(self.device_port.address, "10.200.1.1")


class PortProfileTemplateLockTests(TestCase):
    """ADR 0010: a profile's type ports (and its declared port_count) lock
    once the profile has any instance — layout changes require a new named
    profile instead.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_adding_switch_type_port_blocked_once_instance_exists(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        with self.assertRaises(ValidationError):
            NetworkSwitchTypePort.objects.create(
                switch_type=switch_type, port_number=2, port_type=PortType.GBE_RJ45
            )

    def test_editing_switch_type_port_blocked_once_instance_exists(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        type_port = switch_type.type_ports.get()
        type_port.description = "changed"
        with self.assertRaises(ValidationError):
            type_port.save()

    def test_deleting_switch_type_port_blocked_once_instance_exists(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        type_port = switch_type.type_ports.get()
        with self.assertRaises(ValidationError):
            type_port.delete()

    def test_switch_type_port_count_locked_once_instance_exists(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        switch_type.port_count = 2
        with self.assertRaises(ValidationError):
            switch_type.save()

    def test_device_type_port_locked_once_instance_exists(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        NetworkDevice.objects.create(device_type=device_type)
        type_port = device_type.type_ports.get()
        with self.assertRaises(ValidationError):
            type_port.delete()

    def test_switch_type_port_editable_before_any_instance(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        type_port = switch_type.type_ports.get()
        type_port.description = "fine to edit"
        type_port.save()  # must not raise

    def test_switch_type_port_count_readonly_in_admin_once_instance_exists(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        admin = NetworkSwitchTypeAdmin(NetworkSwitchType, AdminSite())
        self.assertIn("port_count", admin.get_readonly_fields(RequestFactory().get("/"), switch_type))

    def test_switch_type_port_count_editable_in_admin_before_any_instance(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        admin = NetworkSwitchTypeAdmin(NetworkSwitchType, AdminSite())
        self.assertNotIn("port_count", admin.get_readonly_fields(RequestFactory().get("/"), switch_type))

    def test_device_type_port_count_readonly_in_admin_once_instance_exists(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        NetworkDevice.objects.create(device_type=device_type)
        admin = NetworkDeviceTypeAdmin(NetworkDeviceType, AdminSite())
        self.assertIn("port_count", admin.get_readonly_fields(RequestFactory().get("/"), device_type))

    def test_switch_type_identity_readonly_in_admin_once_instance_exists(self) -> None:
        """Review-council finding: the model locks manufacturer/model/name
        alongside port_count once a profile has instances, but the admin
        only marked port_count readonly — an admin could attempt an edit
        the model would then reject.
        """
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        admin = NetworkSwitchTypeAdmin(NetworkSwitchType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), switch_type)
        self.assertIn("manufacturer", readonly)
        self.assertIn("model", readonly)
        self.assertIn("name", readonly)

    def test_device_type_identity_readonly_in_admin_once_instance_exists(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        NetworkDevice.objects.create(device_type=device_type)
        admin = NetworkDeviceTypeAdmin(NetworkDeviceType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), device_type)
        self.assertIn("manufacturer", readonly)
        self.assertIn("model", readonly)
        self.assertIn("name", readonly)

    def test_two_new_device_type_ports_in_one_submission_get_distinct_ordinals(self) -> None:
        """Review-council/Codex finding: admin formsets run every row's
        clean() before any row is saved, so two new ports added to the same
        unlocked profile in one submission both used to compute the same
        "next" ordinal via clean() and collide on
        unique_device_type_port_ordinal at save() time. save() now
        recomputes under the type-row lock instead of trusting clean()'s
        pre-save guess.
        """
        device_type = _make_device_type(port_count=0)
        port_a = NetworkDeviceTypePort(
            device_type=device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        port_b = NetworkDeviceTypePort(
            device_type=device_type, description="Port B", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        # Simulate an admin formset: every row's clean() runs before any
        # row is saved.
        port_a.full_clean()
        port_b.full_clean()
        port_a.save()
        port_b.save()  # must not raise IntegrityError
        self.assertEqual(port_a.ordinal, 1)
        self.assertEqual(port_b.ordinal, 2)

    def test_switch_type_port_delete_checks_persisted_parent_not_reassigned_one(self) -> None:
        """Review-council/Codex finding: delete() used the in-memory
        ``switch_type`` (not the persisted one) to decide whether the lock
        applies, so reassigning a locked type-port to a different, unlocked
        profile in memory and then calling delete() bypassed the lock and
        deleted the row out from under its real, locked parent.
        """
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(switch_type=switch_type)
        type_port = switch_type.type_ports.get()
        other_type = _make_switch_type(port_count=0, name="Other")

        type_port.switch_type = other_type
        with self.assertRaises(ValidationError):
            type_port.delete()
        self.assertEqual(switch_type.type_ports.count(), 1)

    def test_device_type_port_delete_checks_persisted_parent_not_reassigned_one(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        NetworkDevice.objects.create(device_type=device_type)
        type_port = device_type.type_ports.get()
        other_type = _make_device_type(port_count=0, vlan=self.vlan, name="Other")

        type_port.device_type = other_type
        with self.assertRaises(ValidationError):
            type_port.delete()
        self.assertEqual(device_type.type_ports.count(), 1)


class DeleteThenResaveMaterializationTests(TestCase):
    """Review-council/Codex finding: ``Model.delete()`` resets ``pk`` to
    ``None`` but never resets ``_state.adding`` back to ``True``, so a
    plain ``self._state.adding`` check alone would (after this PR's own
    earlier fix from ``self.pk is None`` to ``self._state.adding``)
    incorrectly treat a re-``save()`` of the same in-memory instance as
    "not new" — skipping the lock, validation, and materialization, and
    silently inserting a switch/device with zero ports. ``is_new`` now
    checks both.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_switch_rematerializes_ports_after_delete_and_resave(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        switch.delete()
        self.assertIsNone(switch.pk)
        switch.save()
        self.assertEqual(switch.ports.count(), 1)

    def test_device_rematerializes_ports_after_delete_and_resave(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        device = NetworkDevice.objects.create(device_type=device_type)
        device.delete()
        self.assertIsNone(device.pk)
        device.save()
        self.assertEqual(device.ports.count(), 1)


class ChangeformSaveErrorHandlingTests(TestCase):
    """Review-council finding: some of this app's invariants (locked-field
    and profile-lock checks) are enforced inside ``Model.save()``/
    ``delete()`` themselves, not only ``clean()`` — by design, since a few
    can only be detected at save time (row-locking against a concurrent
    edit, an ordinal collision). Django's admin only turns
    ``clean()``-raised errors into a form error automatically; anything a
    guard raises later, from ``save()``/``delete()`` itself, used to
    propagate straight past the admin's normal error handling as an
    unhandled 500. ``AuditedModelAdminMixin.changeform_view`` now catches
    that and redirects with a message instead.
    """

    def _stub_admin(self, exc: Exception) -> AuditedModelAdminMixin:
        class _StubBase:
            def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
                raise exc

        class _StubAdmin(AuditedModelAdminMixin, _StubBase):
            pass

        return _StubAdmin()

    def _request_with_messages(self):
        request = RequestFactory().post("/admin/inventory/networkswitchtype/1/change/")
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_validation_error_becomes_redirect_with_message(self) -> None:
        admin = self._stub_admin(ValidationError("profile is locked"))
        request = self._request_with_messages()
        response = admin.changeform_view(request, "1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("profile is locked" in str(m) for m in get_messages(request)))

    def test_integrity_error_becomes_redirect_with_message(self) -> None:
        admin = self._stub_admin(IntegrityError("duplicate entry"))
        request = self._request_with_messages()
        response = admin.changeform_view(request, "1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(any("duplicate entry" in str(m) for m in get_messages(request)))


class MaterializedPortLockTests(TestCase):
    """ADR 0010: a materialized instance port's ownership/identity/
    provenance fields are locked after creation — a plain ``save()`` must
    not be able to move a port to another switch/device, renumber it, or
    reorder it (PR #17 review).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = _make_switch_type(port_count=1)
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        self.other_switch = NetworkSwitch.objects.create(
            switch_type=_make_switch_type(port_count=1, name="Other")
        )
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan)
        self.device = NetworkDevice.objects.create(device_type=self.device_type)
        self.other_device = NetworkDevice.objects.create(
            device_type=_make_device_type(port_count=1, vlan=self.vlan, name="Other")
        )

    def test_switch_port_switch_locked(self) -> None:
        port = self.switch.ports.get()
        port.switch = self.other_switch
        with self.assertRaises(ValidationError):
            port.save()

    def test_switch_port_number_locked(self) -> None:
        port = self.switch.ports.get()
        port.port_number = 99
        with self.assertRaises(ValidationError):
            port.save()

    def test_switch_port_source_type_port_locked(self) -> None:
        port = self.switch.ports.get()
        port.source_type_port = self.other_switch.switch_type.type_ports.get()
        with self.assertRaises(ValidationError):
            port.save()

    def test_switch_port_vlan_purpose_still_editable(self) -> None:
        port = self.switch.ports.get()
        port.description = "uplink"
        port.save()  # must not raise

    def test_device_port_device_locked(self) -> None:
        port = self.device.ports.get()
        port.device = self.other_device
        with self.assertRaises(ValidationError):
            port.save()

    def test_device_port_number_locked(self) -> None:
        port = self.device.ports.get()
        port.port_number = 99
        with self.assertRaises(ValidationError):
            port.save()

    def test_device_port_ordinal_locked(self) -> None:
        port = self.device.ports.get()
        port.ordinal = 99
        with self.assertRaises(ValidationError):
            port.save()

    def test_device_port_source_type_port_locked(self) -> None:
        port = self.device.ports.get()
        port.source_type_port = self.other_device.device_type.type_ports.get()
        with self.assertRaises(ValidationError):
            port.save()

    def test_switch_port_number_readonly_in_admin(self) -> None:
        self.assertIn("port_number", NetworkSwitchPortInline.readonly_fields)

    def test_device_port_number_readonly_in_admin(self) -> None:
        self.assertIn("port_number", NetworkDevicePortInline.readonly_fields)

    def test_device_port_dhcp_and_address_still_editable(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.device.rack = rack
        self.device.rack_slot = 1
        self.device.save()
        port = self.device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()  # must not raise


class DerivedDefaultGatewayTests(TestCase):
    """ADR 0010: a device port's default_gateway is a read-only property
    live-derived from its VLAN — never stored, so it can't go stale.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(
            name="Control", vlan_id=200, subnet="10.200.0.0/21", default_gateway="10.200.0.1"
        )
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan)

    def test_gateway_none_while_dhcp(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type)
        port = device.ports.get()
        self.assertTrue(port.is_dhcp)
        self.assertIsNone(port.default_gateway)

    def test_gateway_derived_from_vlan_once_static(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1)
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()
        self.assertEqual(port.default_gateway, "10.200.0.1")

    def test_gateway_follows_later_vlan_gateway_change(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1)
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()
        self.vlan.default_gateway = "10.200.0.254"
        self.vlan.save()
        port.refresh_from_db()
        self.assertEqual(port.default_gateway, "10.200.0.254")


class VLANRemovalViaAllowedVlansTests(TestCase):
    """ADR 0010 / ADR 0007: a VLAN referenced only via a type-port or
    instance-port's ``allowed_vlans`` must still block removal — the
    explicit through model's ``PROTECT`` FK is what makes this hold for
    both single-object and bulk/queryset deletion (review finding #11 — a
    plain M2M's auto-generated join table can't protect at all).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")

    def test_vlan_removal_blocked_by_type_port_allowed_vlans(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        type_port = switch_type.type_ports.get()
        type_port.allowed_vlans.add(self.vlan)
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_by_instance_port_allowed_vlans(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        port.allowed_vlans.add(self.vlan)
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_via_bulk_queryset_delete(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        port.allowed_vlans.add(self.vlan)
        with self.assertRaises(ProtectedError):
            VLAN.objects.filter(pk=self.vlan.pk).delete()
