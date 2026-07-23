"""Tests for the invariants raised in PR #1 review — rack-slot validity,
device-port identity/wiring, and admin-populated audit fields.

These deliberately include direct-ORM writes (``bulk_create``, ``.create()``)
that skip ``full_clean()``, since ``Model.clean()`` is not invoked by
``save()`` — only a DB-level ``CheckConstraint`` can guard those paths.
"""

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase

from .admin import NetworkDeviceAdmin, VLANAdmin
from .models import (
    VLAN,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkSwitch,
    NetworkSwitchPort,
    NetworkSwitchType,
    Rack,
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
