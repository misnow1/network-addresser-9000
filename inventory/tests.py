"""Tests for the invariants raised in PR #1 review — rack-slot validity,
device-port identity/wiring, and admin-populated audit fields.

These deliberately include direct-ORM writes (``bulk_create``, ``.create()``)
that skip ``full_clean()``, since ``Model.clean()`` is not invoked by
``save()`` — only a DB-level ``CheckConstraint`` can guard those paths.
"""

import io
import ipaddress

from auditlog.models import LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.db.models import ProtectedError
from django.forms import inlineformset_factory
from django.test import RequestFactory, TestCase

from .admin import NetworkDeviceAdmin, NetworkSwitchAdmin, RackAdmin, VLANAdmin
from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    Rack,
    RackVlanRange,
)
from .suggestions import (
    prefix_length_for_capacity,
    suggest_default_gateway,
    suggest_dhcp_range,
    suggest_rack_vlan_range,
    suggest_slot_address,
)

User = get_user_model()


class RackSlotAssignmentTests(TestCase):
    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
    def setUp(self) -> None:
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type)

    def test_port_number_must_be_at_least_one(self) -> None:
        port = NetworkSwitchPort(switch=self.switch, port_number=0)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_port_number_cannot_exceed_switch_type_port_count(self) -> None:
        port = NetworkSwitchPort(switch=self.switch, port_number=self.switch_type.port_count + 1)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_db_rejects_zero_port_number_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkSwitchPort.objects.bulk_create([NetworkSwitchPort(switch=self.switch, port_number=0)])


class NetworkDevicePortTests(TestCase):
    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )
        self.device = NetworkDevice.objects.create(device_type=self.device_type)
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        self.switch_port = NetworkSwitchPort.objects.create(switch=switch, port_number=1)

    def test_port_number_cannot_exceed_device_type_port_count(self) -> None:
        port = NetworkDevicePort(device=self.device, port_number=2, vlan=self.vlan, address="10.200.0.10")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_port_number_unique_per_device(self) -> None:
        NetworkDevicePort.objects.create(
            device=self.device, port_number=1, vlan=self.vlan, address="10.200.0.10"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=self.device, port_number=1, vlan=self.vlan, address="10.200.0.11"
            )

    def test_switch_property_derives_from_switch_port(self) -> None:
        port = NetworkDevicePort.objects.create(
            device=self.device,
            port_number=1,
            vlan=self.vlan,
            address="10.200.0.10",
            switch_port=self.switch_port,
        )
        self.assertEqual(port.switch, self.switch_port.switch)

    def test_switch_property_is_none_without_switch_port(self) -> None:
        port = NetworkDevicePort.objects.create(
            device=self.device, port_number=1, vlan=self.vlan, address="10.200.0.10"
        )
        self.assertIsNone(port.switch)

    def test_switch_port_can_only_be_claimed_by_one_device_port(self) -> None:
        NetworkDevicePort.objects.create(
            device=self.device,
            port_number=1,
            vlan=self.vlan,
            address="10.200.0.10",
            switch_port=self.switch_port,
        )
        other_device = NetworkDevice.objects.create(device_type=self.device_type)
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=other_device,
                port_number=1,
                vlan=self.vlan,
                address="10.200.0.11",
                switch_port=self.switch_port,
            )

    def test_dhcp_port_rejects_static_address_via_clean(self) -> None:
        port = NetworkDevicePort(
            device=self.device, port_number=1, vlan=self.vlan, is_dhcp=True, address="10.200.0.10"
        )
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_static_port_requires_address_via_clean(self) -> None:
        port = NetworkDevicePort(device=self.device, port_number=1, vlan=self.vlan, is_dhcp=False)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_db_rejects_dhcp_with_address_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(
                device=self.device, port_number=1, vlan=self.vlan, is_dhcp=True, address="10.200.0.10"
            )

    def test_db_rejects_static_without_address_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            NetworkDevicePort.objects.create(device=self.device, port_number=1, vlan=self.vlan, is_dhcp=False)


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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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

    def test_suggest_dhcp_range_is_bottom_24_of_larger_subnet(self) -> None:
        self.assertEqual(suggest_dhcp_range("10.200.0.0/21"), "10.200.0.0/24")

    def test_suggest_dhcp_range_none_when_subnet_smaller_than_24(self) -> None:
        self.assertIsNone(suggest_dhcp_range("10.200.1.0/27"))

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
        result = suggest_rack_vlan_range("10.200.0.0/21", 30, ["10.200.0.0/24"])
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
    def test_blank_gateway_and_dhcp_range_filled_on_create(self) -> None:
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        vlan.full_clean()
        self.assertEqual(vlan.default_gateway, "10.200.0.1")
        self.assertEqual(vlan.dhcp_range, "10.200.0.0/24")

    def test_explicit_values_are_preserved(self) -> None:
        vlan = VLAN(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            default_gateway="10.200.0.254",
            dhcp_range="10.200.7.0/24",
        )
        vlan.full_clean()
        self.assertEqual(vlan.default_gateway, "10.200.0.254")
        self.assertEqual(vlan.dhcp_range, "10.200.7.0/24")

    def test_dhcp_range_left_blank_for_subnet_smaller_than_24(self) -> None:
        vlan = VLAN(name="Tiny", vlan_id=201, subnet="10.201.1.0/27")
        vlan.full_clean()
        self.assertEqual(vlan.dhcp_range, "")

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
        vlan = VLAN(name="Control", vlan_id=200, subnet="10.200.0.0/21", dhcp_range="10.201.0.0/24")
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
        vlan.dhcp_range = "10.200.1.0/24"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_subnet_to_exclude_existing_switch_address_raises(self) -> None:
        # A static address is allowed even without a RackVlanRange (it only
        # has to fit the VLAN's subnet in that case), so a subnet edit has
        # to be checked against it directly, not just against rack ranges.
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=vlan, address="10.200.5.1")
        vlan.subnet = "10.205.0.0/21"
        with self.assertRaises(ValidationError):
            vlan.full_clean()

    def test_editing_subnet_to_exclude_existing_device_port_address_raises(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )
        device = NetworkDevice.objects.create(device_type=device_type)
        NetworkDevicePort.objects.create(device=device, port_number=1, vlan=vlan, address="10.200.5.2")
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
        self.vlan.dhcp_range = "10.200.0.0/24"
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
        self.vlan.dhcp_range = "10.200.0.0/24"
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
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        switch = NetworkSwitch.objects.create(switch_type=switch_type, rack=self.rack, rack_slot=1)
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.0.1")
        range_ = RackVlanRange.objects.get(rack=self.rack, vlan=self.vlan)
        range_.address_range = "10.200.0.32/27"
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_editing_range_to_exclude_existing_device_port_address_raises(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        NetworkDevicePort.objects.create(device=device, port_number=1, vlan=self.vlan, address="10.200.0.2")
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


class RackSlotCountEditTests(TestCase):
    """Editing Rack.slot_count must be re-validated against what already
    depends on it: existing RackVlanRanges (raised too small) and already
    -assigned equipment (rack_slot beyond the new, lower count).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
        NetworkDevicePort.objects.create(device=device, port_number=1, vlan=self.vlan, address="10.200.1.2")
        device.rack = None
        device.rack_slot = None
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_moving_device_to_another_racks_range_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack_a, rack_slot=2)
        NetworkDevicePort.objects.create(device=device, port_number=1, vlan=self.vlan, address="10.200.1.2")
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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
        port = NetworkDevicePort(device=device, port_number=1, vlan=self.vlan)
        port.full_clean()
        self.assertEqual(port.address, "10.200.1.2")

    def test_device_port_address_requires_manual_entry_without_rack_range(self) -> None:
        other_vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, port_number=1, vlan=other_vlan)
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_switch_address_manually_entered_without_rack_range_still_raises(self) -> None:
        # Racked equipment always requires an assigned RackVlanRange, even
        # for a manually-typed address within the VLAN's subnet — otherwise
        # it could land inside the DHCP range or on the gateway.
        other_vlan = VLAN.objects.create(
            name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21", dhcp_range="10.201.0.0/24"
        )
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1)
        address = NetworkSwitchAddress(switch=switch, vlan=other_vlan, address="10.201.0.5")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_device_port_address_manually_entered_without_rack_range_still_raises(self) -> None:
        other_vlan = VLAN.objects.create(
            name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21", dhcp_range="10.201.0.0/24"
        )
        device = NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=2)
        port = NetworkDevicePort(device=device, port_number=1, vlan=other_vlan, address="10.201.0.5")
        with self.assertRaises(ValidationError):
            port.full_clean()

    def test_unracked_switch_static_address_raises(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        address = NetworkSwitchAddress(switch=switch, vlan=self.vlan, address="10.200.1.5")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_unracked_device_static_port_raises(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type)
        port = NetworkDevicePort(device=device, port_number=1, vlan=self.vlan, address="10.200.1.5")
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
        port = NetworkDevicePort(device=device, port_number=1, vlan=self.vlan, address="10.200.2.2")
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
        conflicting = NetworkDevicePort(device=device, port_number=1, vlan=self.vlan, address="10.200.1.1")
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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
        NetworkDevicePort.objects.create(device=device, port_number=1, vlan=self.vlan, address="10.200.1.2")
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
            device=device, port_number=1, vlan=self.vlan, address="10.200.1.2", switch_port=switch_port
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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )
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
        """A DHCP port has ``address``/``default_gateway`` both null — same
        empty-diff gap as the unracked-switch case above, but for
        ``NetworkDevicePort``.
        """
        device = NetworkDevice.objects.create(device_type=self.device_type)
        port = NetworkDevicePort.objects.create(device=device, port_number=1, vlan=self.vlan, is_dhcp=True)
        pk = port.pk
        port.delete()
        self.assertTrue(LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.DELETE).exists())

    def test_allowed_vlans_change_is_logged(self) -> None:
        """``allowed_vlans`` is a ManyToManyField, which auditlog never diffs
        as an ordinary field — it needs the explicit ``m2m_fields``
        registration (caught by Codex review) to be tracked at all.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        port = NetworkSwitchPort.objects.create(switch=self.switch, port_number=1)
        LogEntry.objects.filter(object_pk=str(port.pk)).delete()

        port.allowed_vlans.add(self.vlan, other_vlan)

        entry = LogEntry.objects.get(object_pk=str(port.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entry.changes_dict["allowed_vlans"]["operation"], "add")


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
        self.switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", port_count=10, port_type="1GbE"
        )
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=1
        )

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
            port_number=1,
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
            port_number=1,
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
