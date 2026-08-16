"""Tests for the invariants raised in PR #1 review — rack-slot validity,
device-port identity/wiring, and admin-populated audit fields — plus the
port-profile/materialization invariants added in phase 8 (ADR 0010).

These deliberately include direct-ORM writes (``bulk_create``, ``.create()``)
that skip ``full_clean()``, since ``Model.clean()`` is not invoked by
``save()`` — only a DB-level ``CheckConstraint``, or an explicit guard
inside ``save()`` itself, can guard those paths.
"""

import importlib
import io
import ipaddress
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest import mock

from auditlog.context import set_actor
from auditlog.models import LogEntry
from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.messages import get_messages
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.models import ProtectedError
from django.forms import inlineformset_factory
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from . import models as inventory_models
from .admin import (
    AuditedModelAdminMixin,
    DepartmentAdmin,
    NetworkDeviceAddForm,
    NetworkDeviceAdmin,
    NetworkDeviceChangeForm,
    NetworkDevicePortForm,
    NetworkDevicePortInline,
    NetworkDeviceTypeAdmin,
    NetworkDeviceTypePortInline,
    NetworkSwitchAddForm,
    NetworkSwitchAddressInline,
    NetworkSwitchAdmin,
    NetworkSwitchPortForm,
    NetworkSwitchPortInline,
    NetworkSwitchTypeAdmin,
    RackAddForm,
    RackAdmin,
    RackTemplateForm,
    RackVlanRangeInlineFormSet,
    SwitchPortVlanProfileAdmin,
    SwitchPortVlanProfileForm,
    VLANAdmin,
)
from .models import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_VLAN_ID,
    DEFAULT_VLAN_NAME,
    VLAN,
    Department,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    NetworkSwitchTypePort,
    Owner,
    PortAddressing,
    PortAddressSource,
    PortMode,
    PortType,
    Rack,
    RackTemplate,
    RackTemplateVlan,
    RackVlanRange,
    SwitchAddressing,
    SwitchPortVlanProfile,
    SwitchPortVlanProfileAllowedVlan,
    _lock_devices_by_pk,
    default_switch_port_vlan_profile,
)
from .suggestions import (
    dhcp_range_overlaps_cidr,
    lowest_free_run,
    prefix_length_for_capacity,
    required_block_size,
    suggest_default_gateway,
    suggest_rack_vlan_range,
    suggest_slot_address,
)
from .validators import validate_dns_label

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


def _make_profile(native_vlan: VLAN, **kwargs) -> SwitchPortVlanProfile:
    """Create an ordinary (non-system) ``SwitchPortVlanProfile`` (ADR 0012)."""
    kwargs.setdefault("name", "Test Profile")
    kwargs.setdefault("port_mode", PortMode.TRUNK)
    return SwitchPortVlanProfile.objects.create(native_vlan=native_vlan, **kwargs)


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
        self.assertEqual(formset.forms[0].instance.address_range, "10.200.0.0/27")


class UnsavedParentInlineSuggestionTests(TestCase):
    """Suggestions must work when adding a switch/device and its address
    inline together against an unsaved parent instance — not just when
    editing an already-saved one. See test above for the equivalent
    RackVlanRange case and its explanation of why this is otherwise broken.

    These build the formset directly rather than going through the admin,
    so they exercise the suggestion path regardless of admin permissions.
    For the switch case, that's no longer "one admin Add page" in practice:
    ADR 0016's ``NetworkSwitchAddressInline.has_add_permission()`` blocks
    adding an address inline on the switch's actual Add page (materialization
    already claims each rack range's VLAN there, and adding one by hand too
    would collide) — MANUAL creation followed by a two-step add on the
    change page is how an operator does this for real.
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
        # 1 slot needs the base address, slot 1, and a reserved top address:
        # 3 addresses, which would round up to the next power of two (/30, 4
        # addresses) on the raw arithmetic alone — but ADR 0015's /27 floor
        # (32 addresses) applies to every slot count this small.
        self.assertEqual(prefix_length_for_capacity(1), 27)

    def test_prefix_length_for_capacity_larger_rack(self) -> None:
        self.assertEqual(prefix_length_for_capacity(62), 26)

    def test_prefix_length_for_capacity_reserves_top_address(self) -> None:
        # A naive "slot_count + 1" rule (no reserved top address) would give
        # slot_count=31 a /27: 31 slots + 1 base = 32 addresses, exactly a
        # /27's worth, putting slot 31 on that block's own top/broadcast-like
        # address. Reserving the top address too means 31 slots need 33
        # addresses, one more than a /27 has, pushing the answer out to a
        # /26. This has to be checked above the /27 floor (ADR 0015) — at or
        # below it (e.g. the old slot_count=3), the floor decides the answer
        # regardless of whether the top address is reserved, so the test
        # would no longer prove anything about the reservation itself.
        self.assertEqual(prefix_length_for_capacity(31), 26)

    def test_required_block_size_and_prefix_floored_below_slot_count_30(self) -> None:
        # ADR 0015: production allocates a uniform /27 per rack regardless of
        # occupancy, and replaying production's honest slot counts (1-19)
        # through the suggester only reproduces that if every slot count up
        # to 30 floors to the same 32-address block. Pin the floor across
        # its whole range, and pin the boundary where it stops applying —
        # slot_count=31 needs 33 addresses, one more than a /27 has, so it's
        # the first count the floor doesn't decide.
        for slot_count in range(1, 31):
            self.assertEqual(required_block_size(slot_count), 32)
            self.assertEqual(prefix_length_for_capacity(slot_count), 27)
        self.assertEqual(required_block_size(31), 33)
        self.assertEqual(prefix_length_for_capacity(31), 26)

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

    # -- lowest_free_run (ADR 0019) ------------------------------------------------------

    def test_lowest_free_run_empty_rack(self) -> None:
        self.assertEqual(lowest_free_run([], 1, 4), 1)

    def test_lowest_free_run_skips_a_gap_too_small(self) -> None:
        # A single free slot at 3 can't fit a span of 2 — the next fit is 5.
        self.assertEqual(lowest_free_run([(1, 2), (4, 4)], 2, 6), 5)

    def test_lowest_free_run_unsorted_and_overlapping_input_matches_normalised(self) -> None:
        normalised = lowest_free_run([(1, 2), (4, 4)], 2, 6)
        # Same occupancy, given unsorted and with a redundant overlapping
        # duplicate thrown in — callers union two tables and shouldn't have
        # to normalise first.
        unsorted_overlapping = lowest_free_run([(4, 4), (1, 2), (1, 1)], 2, 6)
        self.assertEqual(unsorted_overlapping, normalised)

    def test_lowest_free_run_span_larger_than_slot_count_is_none(self) -> None:
        self.assertIsNone(lowest_free_run([], 5, 4))

    def test_lowest_free_run_none_when_run_would_overrun_the_end(self) -> None:
        # slot_count=4, span=2: only a run starting at 3 would fit the end
        # (3-4); occupying 3 leaves no room for a span of 2 anywhere.
        self.assertIsNone(lowest_free_run([(1, 1), (3, 3)], 2, 4))

    def test_lowest_free_run_exact_terminal_fit(self) -> None:
        # occupied=[(1,2)], span=2, slot_count=4: the only fit is exactly
        # 3-4, ending exactly at slot_count — the case that distinguishes
        # `<` from `<=` at the boundary.
        self.assertEqual(lowest_free_run([(1, 2)], 2, 4), 3)


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
        # Pre-existing bug, found while implementing ADR 0015: this used to
        # build a /28 (16 addresses) against a 30-slot rack, but
        # required_block_size(30) was already 32 before the floor existed
        # (30 + 2) — so _validate_range() rejected the range for being too
        # small before ever reaching the overlap loop, and the bare
        # assertRaises(ValidationError) below was passing for the wrong
        # reason. The /27 floor (ADR 0015) makes that permanent. Nudging the
        # /28 up to a /27 at the same base doesn't fix it either —
        # 10.200.0.16/27 has host bits set, and IPv4Network(...,
        # strict=True) rejects it before any overlap check runs. Use a /26
        # instead: comfortably above the floor, and it contains the sibling
        # /27, so it genuinely overlaps.
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        other_rack = Rack.objects.create(name="Rack 2", slot_count=30)
        range_ = RackVlanRange(rack=other_rack, vlan=self.vlan, address_range="10.200.0.0/26")
        with self.assertRaises(ValidationError) as ctx:
            range_.full_clean()
        self.assertIn("overlaps", str(ctx.exception))
        self.assertIn("10.200.0.0/27", str(ctx.exception))

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

    def test_explicit_range_below_floor_raises(self) -> None:
        # A /30 has 4 addresses — smaller than the /27 floor (32 addresses,
        # ADR 0015) that applies at every slot_count, so this is rejected
        # regardless of how small the rack actually is. Split out from the
        # slot-count case below: before the floor existed, a 4-slot rack's
        # own capacity (needs 6 addresses) was already enough to reject a
        # /30 on its own, so that single test covered both reasons at once —
        # now that the floor covers every slot_count up to 30, a rack this
        # small can no longer exercise the slot-count branch at all.
        four_slot_rack = Rack.objects.create(name="Rack 2", slot_count=4)
        range_ = RackVlanRange(rack=four_slot_rack, vlan=self.vlan, address_range="10.200.0.0/30")
        with self.assertRaises(ValidationError) as ctx:
            range_.full_clean()
        self.assertIn("it needs 32 addresses", str(ctx.exception))

    def test_explicit_range_too_small_for_rack_slot_count_raises(self) -> None:
        # Above the /27 floor, a range still has to be sized to the rack's
        # actual slot_count: a /27 (32 addresses) comfortably covers racks up
        # to slot_count 30, but a 40-slot rack needs 42 (slots 1..40, plus
        # the block's own base and top addresses reserved) — the floor no
        # longer covers it, so this exercises the slot-count branch of
        # _validate_range() rather than the floor.
        forty_slot_rack = Rack.objects.create(name="Rack 2", slot_count=40)
        range_ = RackVlanRange(rack=forty_slot_rack, vlan=self.vlan, address_range="10.200.0.0/27")
        with self.assertRaises(ValidationError) as ctx:
            range_.full_clean()
        self.assertIn("it needs 42 addresses", str(ctx.exception))

    def test_editing_range_to_exclude_existing_switch_address_raises(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.0.0/27")
        switch_type = _make_switch_type()
        # MANUAL — the default STATIC choice would already materialize this
        # VLAN's address at creation (ADR 0016), colliding with the explicit
        # create() below on unique_switch_vlan_address.
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=switch_type,
            rack=self.rack,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
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
        # Restructured around a /27 (ADR 0015): a /29 fixture for a 4-slot
        # rack, built via objects.create() (which skips clean()/full_clean()
        # and so bypasses _validate_range()), is now invalid at construction
        # — every rack's smallest possible range is a /27, floor included.
        # A /27 has 32 addresses: room for a 4-slot rack (needs 6, floored to
        # 32) but not a 40-slot one (needs 42).
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=rack, vlan=self.vlan, address_range="10.200.1.0/27")
        rack.slot_count = 40
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
        # MANUAL: this test is about re-validating an already-existing
        # address after the switch moves, not about materialization — the
        # default STATIC choice would materialize this same VLAN's address
        # itself, and the explicit create() below would then collide with
        # it on unique_switch_vlan_address (ADR 0016).
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack_a,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        switch.rack = None
        switch.rack_slot = None
        with self.assertRaises(ValidationError):
            switch.full_clean()

    def test_moving_switch_to_another_racks_range_raises(self) -> None:
        # MANUAL — see test above.
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack_a,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
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
        # MANUAL — this test builds its own NetworkSwitchAddress by hand to
        # exercise the suggestion path directly; the default STATIC choice
        # would already have materialized this VLAN's address at creation
        # (ADR 0016), colliding with it on unique_switch_vlan_address.
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
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
        # MANUAL on both — the default STATIC choice would materialize this
        # VLAN's address on each switch at creation (ADR 0016), so the
        # explicit create()/conflicting-instance below would collide on
        # unique_switch_vlan_address (switch+vlan) instead of exercising
        # the (vlan, address) collision this test is actually about.
        switch_a = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
        switch_b = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=2,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=switch_a, vlan=self.vlan, address="10.200.1.1")
        conflicting = NetworkSwitchAddress(switch=switch_b, vlan=self.vlan, address="10.200.1.1")
        with self.assertRaises(ValidationError):
            conflicting.full_clean()

    def test_device_port_address_cannot_collide_with_switch_address_on_same_vlan(self) -> None:
        # MANUAL — see test above.
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
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

    def test_profile_allowed_vlans_change_is_logged(self) -> None:
        """``SwitchPortVlanProfile.allowed_vlans`` is a ManyToManyField,
        which auditlog never diffs as an ordinary field — it needs the
        explicit ``m2m_fields`` registration (ADR 0012, following the same
        pattern ADR 0010 established for the old per-port ``allowed_vlans``)
        to be tracked at all.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Audio Trunk")
        LogEntry.objects.filter(object_pk=str(profile.pk)).delete()

        profile.allowed_vlans.add(other_vlan)

        entry = LogEntry.objects.get(object_pk=str(profile.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entry.changes_dict["allowed_vlans"]["operation"], "add")

    def test_profile_scalar_edit_is_logged(self) -> None:
        profile = _make_profile(self.vlan, name="Audio Trunk")
        LogEntry.objects.filter(object_pk=str(profile.pk)).delete()

        profile.name = "Renamed Trunk"
        profile.save()

        entries = LogEntry.objects.filter(object_pk=str(profile.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entries.count(), 1)
        self.assertIn("name", entries.first().changes_dict)


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

    def test_switch_type_port_profile_change_is_logged(self) -> None:
        """``profile`` is an ordinary scalar FK on the type port now (ADR
        0012) — no ``m2m_fields`` needed, unlike the old ``allowed_vlans``
        it replaced; full tracking (this model has no ``include_fields``
        scoping) covers it already.
        """
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default", port_count=0
        )
        type_port = NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        LogEntry.objects.filter(object_pk=str(type_port.pk)).delete()
        type_port.profile = _make_profile(self.vlan, name="Audio Trunk")
        type_port.save()
        entry = LogEntry.objects.get(object_pk=str(type_port.pk), action=LogEntry.Action.UPDATE)
        self.assertIn("profile", entry.changes_dict)

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

    def test_unracked_device_materializes_ports_as_dhcp(self) -> None:
        # Unracked (decision 3, ADR 0013) — not "DHCP is the default" anymore;
        # see StaticPortAddressingTests for the racked-static-by-default case.
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

    def test_switch_port_profile_matches_type_port(self) -> None:
        """ADR 0012: the materialized port's ``profile`` is the same profile
        the type port pointed at — a live reference (the id), not a copy of
        the profile's contents.
        """
        switch_type = _make_switch_type(port_count=1)
        type_port = switch_type.type_ports.get()
        profile = _make_profile(self.vlan_a, name="Audio Trunk")
        type_port.profile = profile
        type_port.save()
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        self.assertEqual(port.profile, profile)

    def test_editing_profile_after_materialization_is_visible_through_port(self) -> None:
        """The live-reference contract (ADR 0012): unlike every other
        materialized field, a profile edit made *after* the switch already
        exists still reaches its ports — nothing was copied at
        materialization time, only the id.
        """
        switch_type = _make_switch_type(port_count=1)
        type_port = switch_type.type_ports.get()
        profile = _make_profile(self.vlan_a, name="Audio Trunk")
        type_port.profile = profile
        type_port.save()
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()

        profile.allowed_vlans.add(self.vlan_b)

        port.refresh_from_db()
        self.assertIn(self.vlan_b, port.profile.allowed_vlans.all())

    def test_switch_ports_without_explicit_profile_use_default(self) -> None:
        """Direct ORM creation without passing ``profile`` lands on the
        system Default (the callable ``default=``), not just the admin's
        pre-selected value.
        """
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Default-Test", port_count=1
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45
        )
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        self.assertTrue(port.profile.is_system)
        self.assertEqual(port.profile.name, DEFAULT_PROFILE_NAME)


class StaticPortAddressingTests(TestCase):
    """ADR 0013: device creation defaults to static port materialization
    (rack-range-base + rack-slot, one address per VLAN), revising ADR
    0010's always-DHCP rule.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.l2_vlan = VLAN.objects.create(name="L2 Only", vlan_id=999, subnet="")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_b, address_range="10.201.1.0/27")

    def _make_two_vlan_device_type(self, **kwargs) -> NetworkDeviceType:
        """A two-port device type with each port on a different VLAN — the
        shape every device except Switched Mode satisfies.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", port_count=2, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Control", port_type=PortType.GBE_RJ45, vlan=self.vlan_a
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante", port_type=PortType.GBE_RJ45, vlan=self.vlan_b
        )
        return device_type

    def test_racked_static_gets_base_plus_slot_per_own_vlan(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Two VLAN")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Control"].is_dhcp)
        self.assertEqual(ports["Control"].address, "10.200.1.2")
        self.assertFalse(ports["Dante"].is_dhcp)
        self.assertEqual(ports["Dante"].address, "10.201.1.2")

    def test_explicit_dhcp_on_racked_device_stays_dhcp(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Two VLAN DHCP")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, rack=self.rack, rack_slot=2, port_addressing=PortAddressing.DHCP
        )
        for port in device.ports.all():
            self.assertTrue(port.is_dhcp)
            self.assertIsNone(port.address)

    def test_unracked_device_materializes_dhcp_even_with_static_selected(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Unracked")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, port_addressing=PortAddressing.STATIC
        )
        for port in device.ports.all():
            self.assertTrue(port.is_dhcp)
            self.assertIsNone(port.address)

    def test_default_port_addressing_is_static(self) -> None:
        device = NetworkDevice(device_type=self._make_two_vlan_device_type(name="Default Check"))
        self.assertEqual(device.port_addressing, PortAddressing.STATIC)

    def test_port_addressing_accepted_as_create_kwarg(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Kwarg Check")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, port_addressing=PortAddressing.DHCP
        )
        self.assertEqual(device.port_addressing, PortAddressing.DHCP)

    def test_invalid_port_addressing_rejected_not_silently_dhcp(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Invalid Check")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, port_addressing="bogus")  # type: ignore[misc]

    def test_l2_only_port_stays_dhcp_other_port_goes_static(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Mixed VLAN", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Control", port_type=PortType.GBE_RJ45, vlan=self.vlan_a
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="L2 Link", port_type=PortType.GBE_RJ45, vlan=self.l2_vlan
        )
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Control"].is_dhcp)
        self.assertEqual(ports["Control"].address, "10.200.1.2")
        self.assertTrue(ports["L2 Link"].is_dhcp)
        self.assertIsNone(ports["L2 Link"].address)

    def test_duplicate_vlan_type_refused_atomically(self) -> None:
        """Switched Mode's shape — two ports on the same VLAN — has no way
        to give one address to both (decision 5). Exercised via
        ``objects.create()`` directly, the path that never calls
        ``clean()``, so this proves the save-time preflight (not just the
        admin's clean()-time one) catches it.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Shure", model="ULXD4Q", name="Switched", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante A", port_type=PortType.GBE_RJ45, vlan=self.vlan_b
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante B", port_type=PortType.GBE_RJ45, vlan=self.vlan_b
        )
        with self.assertRaises(ValidationError) as ctx:
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        self.assertIn("Dante A", str(ctx.exception))
        self.assertIn("Dante B", str(ctx.exception))
        self.assertFalse(NetworkDevice.objects.filter(device_type=device_type).exists())
        self.assertFalse(NetworkDevicePort.objects.filter(device__device_type=device_type).exists())

    def test_missing_rack_vlan_range_refused_atomically(self) -> None:
        no_range_vlan = VLAN.objects.create(name="No Range", vlan_id=202, subnet="10.202.0.0/21")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="No Range Type", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=no_range_vlan
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        self.assertFalse(NetworkDevice.objects.filter(device_type=device_type).exists())

    def test_collision_with_existing_address_refused_atomically(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Collision Type", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=self.vlan_a
        )
        # Slot 2 on vlan_a suggests 10.200.1.2 — occupy it first via a switch
        # address so the second device's suggestion collides. MANUAL: the
        # default STATIC choice would materialize the switch's own
        # (different) vlan_a address at slot 3 (10.200.1.3), which would
        # collide with the explicit create() below on
        # unique_switch_vlan_address before this test gets to the
        # collision it's actually about.
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=switch_type,
            rack=self.rack,
            rack_slot=3,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan_a, address="10.200.1.2")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        self.assertFalse(NetworkDevice.objects.filter(device_type=device_type).exists())

    def test_admin_add_post_with_static_materializes_static(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminrole", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole", password="testpass123")
        device_type = self._make_two_vlan_device_type(name="Admin Static")

        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": device_type.pk,
                "hostname": "dev1",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "port_addressing": PortAddressing.STATIC,
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        device = NetworkDevice.objects.get(hostname="dev1")
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Control"].is_dhcp)
        self.assertEqual(ports["Control"].address, "10.200.1.2")

    def test_admin_add_post_omitting_port_addressing_still_defaults_to_static(self) -> None:
        """Review-council finding: the field must be ``required=False`` — a
        POST that omits it entirely (any tooling written before this field
        existed, or a client that only sends changed/touched fields) must
        still succeed and default to static, not fail with "this field is
        required" the way a genuinely required model field would.
        """
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminrole4", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole4", password="testpass123")
        device_type = self._make_two_vlan_device_type(name="Admin Omitted Field")

        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": device_type.pk,
                "hostname": "dev4",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                # port_addressing deliberately omitted
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        device = NetworkDevice.objects.get(hostname="dev4")
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Control"].is_dhcp)
        self.assertEqual(ports["Control"].address, "10.200.1.2")

    def test_admin_add_post_duplicate_vlan_renders_form_error_not_500(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminrole2", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole2", password="testpass123")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Shure", model="ULXD4Q", name="Admin Switched", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante A", port_type=PortType.GBE_RJ45, vlan=self.vlan_b
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante B", port_type=PortType.GBE_RJ45, vlan=self.vlan_b
        )

        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": device_type.pk,
                "hostname": "dev2",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "port_addressing": PortAddressing.STATIC,
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-renders with a form error, not a 500
        self.assertContains(response, "Dante A")
        self.assertContains(response, "Dante B")
        self.assertFalse(NetworkDevice.objects.filter(hostname="dev2").exists())

    def test_admin_add_post_address_collision_renders_form_error_not_500(self) -> None:
        """Review-council finding: `_validate_static_address()` raises a
        `ValidationError` keyed on "address" — the right shape for
        `NetworkDevicePort.clean()`, which has that field, but wrong for
        `NetworkDevice.clean()` (and `NetworkDeviceAddForm`, which has no
        `address` field at all). Left unconverted, Django's `add_error()`
        raises a raw `ValueError` for a nonexistent form field instead of
        rendering a validation message — an ordinary Editor submission
        hitting an address collision would 500, not see a form error.
        """
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminrole3", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminrole3", password="testpass123")
        # Slot 2 on vlan_a suggests 10.200.1.2 — occupy it first via a switch
        # address so the device's suggested address collides. MANUAL — see
        # the identical setup in test_collision_with_existing_address_
        # refused_atomically for why the default STATIC choice would
        # collide with the explicit create() below before this test gets
        # to the collision it's actually about.
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=switch_type,
            rack=self.rack,
            rack_slot=3,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan_a, address="10.200.1.2")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Martin Audio", model="IK-42", name="Collision Admin Type", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Port A", port_type=PortType.GBE_RJ45, vlan=self.vlan_a
        )

        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": device_type.pk,
                "hostname": "dev3",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "port_addressing": PortAddressing.STATIC,
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-renders with a form error, not a 500
        self.assertContains(response, "10.200.1.2")
        self.assertFalse(NetworkDevice.objects.filter(hostname="dev3").exists())

    def test_admin_add_form_shows_field_preselected_static(self) -> None:
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        request = RequestFactory().get("/admin/inventory/networkdevice/add/")
        form_class = admin.get_form(request, None)
        self.assertIn("port_addressing", form_class.base_fields)
        self.assertEqual(form_class.base_fields["port_addressing"].initial, PortAddressing.STATIC)

    def test_admin_change_form_omits_field(self) -> None:
        device_type = self._make_two_vlan_device_type(name="Change View")
        device = NetworkDevice.objects.create(device_type=device_type)
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        request = RequestFactory().get(f"/admin/inventory/networkdevice/{device.pk}/change/")
        form_class = admin.get_form(request, device)
        self.assertNotIn("port_addressing", form_class.base_fields)


class SlotOffsetAddressingTests(TestCase):
    """ADR 0017: ``NetworkDeviceTypePort.slot_offset`` (+ its
    ``NetworkDevicePort`` copy) — a materialized offset port's address
    becomes ``range_base + rack_slot + slot_offset``, a device occupies the
    ordinal range its type's max offset implies, and the same-VLAN
    pre-flight narrows from "same VLAN" to "same VLAN and same offset".

    Reuses ``StaticPortAddressingTests``' rack/VLAN fixture shape. Maps
    onto ``PLAN-adr-0017.md``'s 14-case verification list — not 1:1; a few
    cases split into a static/DHCP/unracked trio or a both-directions pair,
    since each half is materially different code (note 4/6 in the plan's
    review response).
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_b, address_range="10.201.1.0/27")

    def _make_sd12_type(self, **kwargs) -> NetworkDeviceType:
        """SD12-shaped type (ADR 0017's motivating example): Control at
        offset 0, Engine at offset 1, both on ``vlan_a`` — the
        console-with-derived-engine shape the ADR exists to represent.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", port_count=2, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=1,
        )
        return device_type

    def _make_orphan_offset_type(self, **kwargs) -> NetworkDeviceType:
        """A VLAN with only an offset-1 port and no offset-0 sibling — the
        shape ``_validate_device_type_port_profile`` must refuse on every
        addressing path (plan review note 4), not only the static one.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Orphan", port_count=1, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Engine Only",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_a,
            slot_offset=1,
        )
        return device_type

    # Case 1: materializes base+slot and base+slot+offset.
    def test_offset_port_materializes_base_plus_slot_plus_offset(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Materialize")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=7)
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Control"].is_dhcp)
        self.assertEqual(ports["Control"].address, "10.200.1.7")
        self.assertFalse(ports["Engine"].is_dhcp)
        self.assertEqual(ports["Engine"].address, "10.200.1.8")
        self.assertEqual(ports["Engine"].slot_offset, 1)

    # Case 2: offset port's address rejected as read-only after creation.
    def test_offset_port_address_rejected_as_readonly_after_creation(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Locked")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        engine = device.ports.get(description="Engine")
        engine.address = "10.200.1.99"
        with self.assertRaises(ValidationError):
            engine.save()

    # Case 3: offset port recomputed when the offset-0 address is edited.
    def test_offset_port_recomputed_when_control_address_edited(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Recompute")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")
        control.address = "10.200.1.5"
        control.save()
        engine = device.ports.get(description="Engine")
        self.assertEqual(engine.address, "10.200.1.6")

    # Case 4: DHCP cascade both ways.
    def test_dhcp_cascade_both_ways(self) -> None:
        device_type = self._make_sd12_type(name="SD12 DHCP Cascade")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        control = device.ports.get(description="Control")

        control.is_dhcp = True
        control.address = None
        control.save()
        engine = device.ports.get(description="Engine")
        self.assertTrue(engine.is_dhcp)
        self.assertIsNone(engine.address)

        control.is_dhcp = False
        control.address = "10.200.1.9"
        control.save()
        engine.refresh_from_db()
        self.assertFalse(engine.is_dhcp)
        self.assertEqual(engine.address, "10.200.1.10")

    # Case 5: rollback — a derived collision rolls back the control edit too.
    def test_rollback_on_derived_collision(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Rollback")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        # Occupy 10.200.1.6 — the address the Engine would derive to once
        # Control moves to .5 — with an unrelated device's port, so the
        # collision below is deliberate, not incidental.
        blocker_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="SD12 Rollback Blocker")
        NetworkDevice.objects.create(device_type=blocker_type, rack=self.rack, rack_slot=6)

        control = device.ports.get(description="Control")
        control.address = "10.200.1.5"
        with self.assertRaises(ValidationError):
            control.save()

        control.refresh_from_db()
        self.assertEqual(control.address, "10.200.1.1")
        self.assertEqual(device.ports.get(description="Engine").address, "10.200.1.2")

    # Case 6: update_fields discipline — a dirty in-memory address must not
    # cascade when update_fields excludes it.
    def test_update_fields_switch_port_only_does_not_cascade(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Update Fields")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        switch_port = switch.ports.get()

        control = device.ports.get(description="Control")
        control.switch_port = switch_port
        control.address = "10.200.1.9"  # dirty in-memory only — must not persist or cascade
        control.save(update_fields=["switch_port"])

        control.refresh_from_db()
        self.assertEqual(control.address, "10.200.1.1")
        self.assertEqual(control.switch_port, switch_port)
        self.assertEqual(device.ports.get(description="Engine").address, "10.200.1.2")

    # Case 7 (both directions): a second occupant refused inside an
    # existing occupant's span.
    def test_device_span_refused_over_existing_switch(self) -> None:
        switch_type = _make_switch_type(port_count=1)
        NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=switch_type,
            rack=self.rack,
            rack_slot=8,
            address_materialization=SwitchAddressing.MANUAL,
        )
        device_type = self._make_sd12_type(name="SD12 Over Switch")
        device = NetworkDevice(device_type=device_type, rack=self.rack, rack_slot=7)  # spans 7-8
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_switch_refused_inside_existing_device_span(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Under Switch")
        NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=7)  # spans 7-8
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch(  # type: ignore[misc]
            switch_type=switch_type,
            rack=self.rack,
            rack_slot=8,
            address_materialization=SwitchAddressing.MANUAL,
        )
        with self.assertRaises(ValidationError):
            switch.full_clean()

    # Case 8: span-query edge cases.
    def test_device_excludes_itself_on_resave(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Resave")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        device.hostname = "renamed"
        device.full_clean()  # must not raise — the occupancy check must not see itself
        device.save()

    def test_type_with_zero_ports_yields_span_one(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Generic", model="Empty", name="Zero Ports", port_count=0
        )
        self.assertEqual(device_type.slot_span, 1)

    def test_unracked_devices_ignored_by_span_occupancy_check(self) -> None:
        unracked_type = self._make_sd12_type(name="SD12 Unracked")
        NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=unracked_type, port_addressing=PortAddressing.DHCP
        )
        other = NetworkDevice(
            device_type=self._make_sd12_type(name="SD12 Unracked Other"), rack=self.rack, rack_slot=1
        )
        other.full_clean()  # must not raise — the unracked device isn't a conflict

    # Case 9/10: the .255 bound, via clean() and via objects.create()
    # (which bypasses clean() entirely).
    def test_span_exceeding_slot_count_refused_via_full_clean(self) -> None:
        # A `.224`-aligned /27, where the failure mode is a `.255` address —
        # ADR 0017's exact motivating scenario for this bound.
        aligned_rack = Rack.objects.create(name="Aligned Rack", slot_count=30)
        RackVlanRange.objects.create(rack=aligned_rack, vlan=self.vlan_a, address_range="10.200.1.224/27")
        device_type = self._make_sd12_type(name="SD12 Boundary Clean")
        device = NetworkDevice(device_type=device_type, rack=aligned_rack, rack_slot=30)  # spans 30-31: .255
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_span_exceeding_slot_count_refused_via_objects_create(self) -> None:
        aligned_rack = Rack.objects.create(name="Aligned Rack 2", slot_count=30)
        RackVlanRange.objects.create(rack=aligned_rack, vlan=self.vlan_a, address_range="10.200.1.224/27")
        device_type = self._make_sd12_type(name="SD12 Boundary Create")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, rack=aligned_rack, rack_slot=30)
        self.assertFalse(NetworkDevice.objects.filter(device_type=device_type).exists())

    # Case 11: Switched Mode (same VLAN, same offset) is still refused —
    # the pre-flight narrowed, not lost, this case.
    def test_switched_mode_still_refused_same_vlan_same_offset(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Shure", model="ULXD4Q", name="Switched Offset Check", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Dante A",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_b,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Dante B",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan_b,
            slot_offset=0,
        )
        with self.assertRaises(ValidationError) as ctx:
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=2)
        self.assertIn("Dante A", str(ctx.exception))
        self.assertIn("Dante B", str(ctx.exception))

    # Case 12: a VLAN with an offset-1 port and no offset-0 port is
    # refused on every addressing path, including DHCP and unracked, which
    # skip the static pre-flight entirely.
    def test_offset_without_offset_zero_refused_static(self) -> None:
        device_type = self._make_orphan_offset_type(name="Orphan Static")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)

    def test_offset_without_offset_zero_refused_dhcp(self) -> None:
        device_type = self._make_orphan_offset_type(name="Orphan DHCP")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(  # type: ignore[misc]
                device_type=device_type, rack=self.rack, rack_slot=1, port_addressing=PortAddressing.DHCP
            )

    def test_offset_without_offset_zero_refused_unracked(self) -> None:
        device_type = self._make_orphan_offset_type(name="Orphan Unracked")
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type)

    # Case 13: a move leaves stored addresses unchanged, and is blocked
    # when the new position would no longer fit — nothing recomputes on a
    # move (ADR 0017's "what does not change").
    def test_move_leaves_addresses_unchanged_and_blocked_if_no_longer_fits(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Move")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control_before = device.ports.get(description="Control").address
        engine_before = device.ports.get(description="Engine").address

        device.rack_slot = 2
        device.full_clean()
        device.save()
        self.assertEqual(device.ports.get(description="Control").address, control_before)
        self.assertEqual(device.ports.get(description="Engine").address, engine_before)

        # slot_count=10: a move to 10 spans 10-11, one past the end.
        device.rack_slot = 10
        with self.assertRaises(ValidationError):
            device.full_clean()
        device.refresh_from_db()
        self.assertEqual(device.rack_slot, 2)
        self.assertEqual(device.ports.get(description="Control").address, control_before)
        self.assertEqual(device.ports.get(description="Engine").address, engine_before)

    # Case 14: admin — the offset row's address widget is disabled, and a
    # POST that tries to smuggle a value past it is ignored.
    def test_admin_offset_row_address_widget_disabled(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Admin Disabled")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        engine = device.ports.get(description="Engine")
        control = device.ports.get(description="Control")
        self.assertTrue(NetworkDevicePortForm(instance=engine).fields["address"].disabled)
        self.assertFalse(NetworkDevicePortForm(instance=control).fields["address"].disabled)

    def test_admin_offset_row_address_post_smuggle_ignored(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Admin Smuggle")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        engine = device.ports.get(description="Engine")
        original_address = engine.address

        form = NetworkDevicePortForm(
            data={
                "description": engine.description,
                "port_number": engine.port_number or "",
                "port_type": engine.port_type,
                "vlan": str(engine.vlan_id),
                "slot_offset": str(engine.slot_offset),
                "address": "10.200.1.250",  # smuggled — the field is disabled, so this must be ignored
            },
            instance=engine,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save()
        self.assertEqual(saved.address, original_address)

    # The cascade must derive from what update_fields actually persisted,
    # not from whatever's dirty in memory on an excluded field.
    def test_update_fields_address_only_ignores_dirty_in_memory_is_dhcp(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Effective Values")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")

        # is_dhcp is dirty in memory only — save(update_fields=["address"])
        # must not let it influence the cascade, since the database's own
        # is_dhcp column is never touched by this save.
        control.address = "10.200.1.5"
        control.is_dhcp = True
        control.save(update_fields=["address"])

        control.refresh_from_db()
        self.assertFalse(control.is_dhcp)
        self.assertEqual(control.address, "10.200.1.5")
        engine = device.ports.get(description="Engine")
        self.assertFalse(engine.is_dhcp)
        self.assertEqual(engine.address, "10.200.1.6")

    # The address lock must key off the *persisted* slot_offset, not a
    # caller-tampered in-memory one.
    def test_locked_field_check_uses_persisted_offset_not_in_memory(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Persisted Offset Lock")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        engine = device.ports.get(description="Engine")

        engine.slot_offset = 0  # tampered in memory — must not evade the address lock
        engine.address = "10.200.1.9"  # otherwise-valid — isolates the lock check specifically
        with self.assertRaises(ValidationError):
            engine.save(update_fields=["address"])

        engine.refresh_from_db()
        self.assertEqual(engine.slot_offset, 1)
        self.assertEqual(engine.address, "10.200.1.2")

    # A single admin submission editing both the control row's address and
    # an offset row's other editable field (switch_port) must not be
    # rejected over a stale formset snapshot.
    def test_formset_save_refreshes_stale_offset_address_before_saving(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Formset Staleness")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")
        engine = device.ports.get(description="Engine")
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        switch_port = switch.ports.get()
        user = User.objects.create_user(username="formset-editor", password="x")

        FormSet = inlineformset_factory(
            NetworkDevice,
            NetworkDevicePort,
            form=NetworkDevicePortForm,
            fields=["is_dhcp", "address", "switch_port"],
            extra=0,
            can_delete=False,
        )
        data = {
            "ports-TOTAL_FORMS": "2",
            "ports-INITIAL_FORMS": "2",
            "ports-MIN_NUM_FORMS": "0",
            "ports-MAX_NUM_FORMS": "1000",
            "ports-0-id": str(control.pk),
            "ports-0-address": "10.200.1.5",  # control edit — must cascade to Engine
            "ports-1-id": str(engine.pk),
            "ports-1-address": engine.address,  # disabled — submitted value is ignored either way
            "ports-1-switch_port": str(switch_port.pk),  # engine's own, unrelated, legitimate edit
        }
        formset = FormSet(data, instance=device, prefix="ports")
        self.assertTrue(formset.is_valid(), formset.errors)

        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        request = RequestFactory().post(f"/admin/inventory/networkdevice/{device.pk}/change/")
        request.user = user
        admin.save_formset(request, form=None, formset=formset, change=True)

        control.refresh_from_db()
        engine.refresh_from_db()
        self.assertEqual(control.address, "10.200.1.5")
        self.assertEqual(engine.address, "10.200.1.6")  # derived, not rejected
        self.assertEqual(engine.switch_port, switch_port)

    # An IPv4 overflow while deriving a sibling's address must raise
    # ValidationError, not a bare ipaddress.AddressValueError (which
    # would 500 the admin).
    def test_derived_overflow_raises_validation_error(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Overflow")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")

        control.address = "255.255.255.255"
        with self.assertRaises(ValidationError):
            control.save()

        control.refresh_from_db()
        self.assertEqual(control.address, "10.200.1.1")
        self.assertEqual(device.ports.get(description="Engine").address, "10.200.1.2")

    # Deleting an offset-0 port must not orphan its offset siblings —
    # single delete, queryset delete, and the whole-device cascade (which
    # must still work in one step).
    def test_delete_offset_zero_port_with_siblings_blocked(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Delete Guard")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")
        with self.assertRaises(ValidationError):
            control.delete()
        self.assertTrue(NetworkDevicePort.objects.filter(pk=control.pk).exists())

    def test_delete_offset_zero_port_with_siblings_blocked_via_queryset(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Delete Guard Bulk")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        with self.assertRaises(ValidationError):
            NetworkDevicePort.objects.filter(device=device, description="Control").delete()
        self.assertTrue(device.ports.filter(description="Control").exists())

    # The delete guard must key off the *persisted* slot_offset/device/
    # vlan, not an in-memory one — delete() has no locked-field validation
    # the way save()/clean() do, so nothing else stops a caller from
    # tampering with an instance's identity fields before calling
    # .delete(). Two vectors: masking an offset-0 row as if it weren't one
    # (slot_offset), and pointing the sibling lookup at an unrelated
    # device/VLAN with nothing to conflict on (device_id/vlan_id) — either
    # way, super().delete() still removes the real row by pk regardless of
    # what's in memory, so a guard that trusts self would let it through.
    def test_delete_guard_uses_persisted_slot_offset_not_in_memory(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Delete Guard Tamper Offset")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")

        control.slot_offset = 1  # tampered — must not make the guard think this isn't offset-0
        with self.assertRaises(ValidationError):
            control.delete()

        self.assertTrue(NetworkDevicePort.objects.filter(pk=control.pk).exists())
        self.assertTrue(device.ports.filter(description="Engine").exists())

    def test_delete_guard_uses_persisted_device_and_vlan_not_in_memory(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Delete Guard Tamper Relations")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        control = device.ports.get(description="Control")
        # An unrelated device/VLAN with no offset siblings at all — if the
        # guard trusted these tampered in-memory ids, it would find no
        # conflict here and let the real (Control's actual) row through.
        other_type = _make_device_type(port_count=1, vlan=self.vlan_b, name="SD12 Delete Guard Tamper Other")
        other_device = NetworkDevice.objects.create(device_type=other_type, rack=self.rack, rack_slot=5)
        other_port = other_device.ports.get()

        control.device_id = other_port.device_id
        control.vlan_id = other_port.vlan_id
        with self.assertRaises(ValidationError):
            control.delete()

        self.assertTrue(NetworkDevicePort.objects.filter(pk=control.pk).exists())
        self.assertTrue(device.ports.filter(description="Engine").exists())

    def test_delete_offset_zero_port_without_siblings_allowed(self) -> None:
        plain_type = _make_device_type(port_count=1, vlan=self.vlan_a, name="SD12 Delete Guard Plain")
        device = NetworkDevice.objects.create(device_type=plain_type, rack=self.rack, rack_slot=5)
        port = device.ports.get()
        port.delete()  # offset 0, no siblings — must not raise
        self.assertFalse(NetworkDevicePort.objects.filter(pk=port.pk).exists())

    def test_deleting_whole_device_cascades_without_blocking(self) -> None:
        device_type = self._make_sd12_type(name="SD12 Delete Guard Cascade")
        device = NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=1)
        device.delete()  # must not raise despite Control having an offset sibling
        self.assertFalse(NetworkDevicePort.objects.filter(device_id=device.pk).exists())


class RetireCompanionsMigrationTests(TransactionTestCase):
    """``0014_retire_companions`` (ADR 0022 PR 2, PLAN-adr-0022.md's
    settled decisions 8-10) — the one genuinely dangerous step in this
    plan: it moves an address between rows, creates a type port, mutates
    ``port_count``, and deletes device and type rows.

    Reconstructs the schema as of ``0013_operator_set_ports`` (immediately
    before this migration) via ``MigrationExecutor`` — real ``ALTER
    TABLE``s, not just Python model classes — since ``host``/
    ``companion_type`` genuinely don't exist in this branch's live schema
    by the time any other test in this suite runs. Every fixture is built
    through the *historical* models this reconstructed state provides
    (never the live, post-migration ones, which have no such fields at
    all and whose ``save()`` would reject the ownership/ordinal changes
    the migration itself makes as locked-field violations), and every
    assertion reads back through the same historical models.

    A ``TransactionTestCase``, not a plain ``TestCase`` — MariaDB's DDL
    isn't transactional, so the schema rollback in ``setUpClass`` can't
    happen inside the outer atomic block an ordinary ``TestCase`` wraps
    every test in. ``tearDownClass`` migrates back to head so every other
    test in the suite sees the real, current schema.
    """

    #: The reconstructed-as-of-0013 historical ``apps`` registry, and the
    #: dynamically-imported migration module under test — both ``Any``,
    #: since neither has a real static type (historical models are built
    #: at runtime; the migration module's name isn't a valid identifier).
    apps: Any
    _migration_module: Any

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        executor = MigrationExecutor(connection)
        executor.migrate([("inventory", "0013_operator_set_ports")])
        executor.loader.build_graph()
        cls.apps = executor.loader.project_state(("inventory", "0013_operator_set_ports")).apps
        cls._migration_module = importlib.import_module("inventory.migrations.0014_retire_companions")

    @classmethod
    def tearDownClass(cls) -> None:
        call_command("migrate", "inventory", verbosity=0)
        super().tearDownClass()

    def setUp(self) -> None:
        self._counter = 0
        self.schema_editor = _FakeSchemaEditor(connection.alias)

    def _unique(self, label: str) -> str:
        self._counter += 1
        return f"{label}-{self._counter}"

    def _next_vlan_id(self) -> int:
        self._counter += 1
        return 900 + self._counter

    def _next_address(self) -> str:
        self._counter += 1
        return f"10.201.6.{self._counter}"

    def _run_migration(self) -> None:
        self._migration_module.retire_companions(self.apps, self.schema_editor)

    def _make_types(self, *, host_port_count: int = 3) -> dict[str, Any]:
        """One host type (``host_port_count`` ports: Control, Dante
        Primary, Dante Secondary, in that order) declaring one companion
        type (a single Dante Primary port) — the production DM7C/DM3
        shape. Returns the types and VLANs; ``_make_device_pair()``
        instantiates devices against them.
        """
        apps = self.apps
        VLAN = apps.get_model("inventory", "VLAN")
        NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
        NetworkDeviceTypePort = apps.get_model("inventory", "NetworkDeviceTypePort")

        vlan_control = VLAN.objects.create(name=self._unique("Control"), vlan_id=self._next_vlan_id())
        vlan_dante = VLAN.objects.create(name=self._unique("Dante Primary"), vlan_id=self._next_vlan_id())

        companion_type = NetworkDeviceType.objects.create(
            manufacturer="Test",
            model=self._unique("Companion"),
            name="Device Control Interface",
            port_count=1,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=companion_type,
            description="Dante Primary",
            port_type="1gbe_rj45",
            vlan=vlan_dante,
            ordinal=1,
        )
        host_type = NetworkDeviceType.objects.create(
            manufacturer="Test",
            model=self._unique("Host"),
            name="Default",
            port_count=host_port_count,
            companion_type=companion_type,
        )
        descriptions = ["Control", "Dante Primary", "Dante Secondary"][:host_port_count]
        for ordinal, description in enumerate(descriptions, start=1):
            NetworkDeviceTypePort.objects.create(
                device_type=host_type,
                description=description,
                port_type="1gbe_rj45",
                vlan=vlan_control if description == "Control" else vlan_dante,
                ordinal=ordinal,
            )
        return {
            "host_type": host_type,
            "companion_type": companion_type,
            "vlan_control": vlan_control,
            "vlan_dante": vlan_dante,
            "descriptions": descriptions,
        }

    def _make_device_pair(
        self, types: dict[str, Any], *, companion_ports: int = 1, companion_is_dhcp: bool = False
    ) -> dict[str, Any]:
        """One host device (materialized by hand, matching ``types``'
        ports) and one companion linked to it — companion_ports/
        companion_is_dhcp let the guard-rejection tests build an
        inadmissible shape without duplicating this whole helper.
        """
        apps = self.apps
        NetworkDevice = apps.get_model("inventory", "NetworkDevice")
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")

        host = NetworkDevice.objects.create(device_type=types["host_type"], hostname=self._unique("host"))
        for ordinal, description in enumerate(types["descriptions"], start=1):
            NetworkDevicePort.objects.create(
                device=host,
                description=description,
                vlan=types["vlan_control"] if description == "Control" else types["vlan_dante"],
                port_type="1gbe_rj45",
                address=self._next_address(),
                ordinal=ordinal,
            )
        companion = NetworkDevice.objects.create(
            device_type=types["companion_type"], hostname=f"{host.hostname}-device-control", host=host
        )
        companion_port = None
        for i in range(companion_ports):
            port = NetworkDevicePort.objects.create(
                device=companion,
                description="Dante Primary" if i == 0 else f"Extra {i}",
                vlan=types["vlan_dante"],
                port_type="1gbe_rj45",
                ordinal=i + 1,
                is_dhcp=companion_is_dhcp,
                address=None if companion_is_dhcp else self._next_address(),
            )
            if i == 0:
                companion_port = port
        return {"host": host, "companion": companion, "companion_port": companion_port}

    def test_moves_the_port_row_rather_than_duplicating_it(self) -> None:
        NetworkDevicePort = self.apps.get_model("inventory", "NetworkDevicePort")
        NetworkDevice = self.apps.get_model("inventory", "NetworkDevice")
        types = self._make_types()
        pair = self._make_device_pair(types)
        companion_port_pk = pair["companion_port"].pk
        companion_address = pair["companion_port"].address

        self._run_migration()

        moved_port = NetworkDevicePort.objects.get(pk=companion_port_pk)  # same row, not a new one
        self.assertEqual(moved_port.device_id, pair["host"].pk)
        self.assertEqual(moved_port.description, "Device Control")
        self.assertEqual(moved_port.address, companion_address)
        self.assertFalse(NetworkDevice.objects.filter(pk=pair["companion"].pk).exists())
        self.assertEqual(NetworkDevicePort.objects.filter(device_id=pair["host"].pk).count(), 4)

    def test_host_type_shared_by_two_devices_gets_one_type_port_and_one_increment(self) -> None:
        NetworkDeviceTypePort = self.apps.get_model("inventory", "NetworkDeviceTypePort")
        NetworkDeviceType = self.apps.get_model("inventory", "NetworkDeviceType")
        NetworkDevicePort = self.apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        pair_1 = self._make_device_pair(types)
        pair_2 = self._make_device_pair(types)

        self._run_migration()

        device_control_ports = NetworkDeviceTypePort.objects.filter(
            device_type_id=types["host_type"].pk, description="Device Control"
        )
        self.assertEqual(device_control_ports.count(), 1)
        new_type_port = device_control_ports.get()
        host_type = NetworkDeviceType.objects.get(pk=types["host_type"].pk)
        self.assertEqual(host_type.port_count, 4)  # incremented once, not twice
        for pair in (pair_1, pair_2):
            moved = NetworkDevicePort.objects.get(pk=pair["companion_port"].pk)
            self.assertEqual(moved.device_id, pair["host"].pk)
            self.assertEqual(moved.source_type_port_id, new_type_port.pk)

    def test_raises_on_a_multi_port_companion_instance(self) -> None:
        # The companion *type* still declares one type port (the shape a
        # correct import always produces) — this instance has two
        # materialized ports anyway, the corruption the per-instance
        # count guard catches independently of the type-level one.
        types = self._make_types()
        self._make_device_pair(types, companion_ports=2)
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_on_a_multi_port_companion_type(self) -> None:
        NetworkDeviceTypePort = self.apps.get_model("inventory", "NetworkDeviceTypePort")
        types = self._make_types()
        self._make_device_pair(types)
        # A second type port on the companion type itself — the shape
        # this migration is explicitly not written to handle (settled
        # decision 9), independent of how many ports any instance has.
        NetworkDeviceTypePort.objects.create(
            device_type=types["companion_type"],
            description="Extra Type Port",
            port_type="1gbe_rj45",
            vlan=types["vlan_dante"],
            ordinal=2,
        )
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_on_a_dhcp_companion_instance(self) -> None:
        types = self._make_types()
        self._make_device_pair(types, companion_is_dhcp=True)
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_on_an_unlinked_companion_instance(self) -> None:
        # An orphaned companion (host deleted out from under it) cannot
        # exist — host is OneToOneField(..., on_delete=CASCADE). The
        # corruption that *can* exist is an unlinked instance of a
        # companion type, reachable only through a direct write that
        # bypasses the live model's own creation-time enforcement — the
        # same documented bypass this file's other corruption tests use,
        # applied here through the historical model instead.
        apps = self.apps
        NetworkDevice = apps.get_model("inventory", "NetworkDevice")
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        unlinked = NetworkDevice.objects.create(
            device_type=types["companion_type"], hostname="orphan-1-device-control", host=None
        )
        NetworkDevicePort.objects.create(
            device=unlinked,
            description="Dante Primary",
            vlan=types["vlan_dante"],
            port_type="1gbe_rj45",
            address=self._next_address(),
            ordinal=1,
        )
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_on_a_host_instance_with_no_companion(self) -> None:
        # A host instance the companion-only guard loop never reaches at
        # all — its companion is simply missing, not merely orphaned or
        # unlinked. Only a guard that separately enumerates host instances
        # (not just companions) can see this (Codex review, P1).
        apps = self.apps
        NetworkDevice = apps.get_model("inventory", "NetworkDevice")
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        host = NetworkDevice.objects.create(device_type=types["host_type"], hostname=self._unique("host"))
        for ordinal, description in enumerate(types["descriptions"], start=1):
            NetworkDevicePort.objects.create(
                device=host,
                description=description,
                vlan=types["vlan_control"] if description == "Control" else types["vlan_dante"],
                port_type="1gbe_rj45",
                address=self._next_address(),
                ordinal=ordinal,
            )
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_on_a_companion_with_an_unexpected_hostname(self) -> None:
        # ADR 0018 permitted a companion named anything at all; settled
        # decision 9 refuses to guess at a shape import_prod_data.py's own
        # "<host>-device-control" convention never produces (Codex review,
        # P1).
        apps = self.apps
        NetworkDevice = apps.get_model("inventory", "NetworkDevice")
        types = self._make_types()
        pair = self._make_device_pair(types)
        NetworkDevice.objects.filter(pk=pair["companion"].pk).update(hostname="totally-unrelated-name")
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_raises_when_the_moved_port_has_drifted_from_its_type_port(self) -> None:
        # The companion instance's own port disagrees with the companion
        # type's sole type port (port_type here) — moving it onto the new
        # Device Control type port would leave a materialized row whose
        # source_type_port describes different hardware (Codex review,
        # P1).
        apps = self.apps
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        pair = self._make_device_pair(types)
        NetworkDevicePort.objects.filter(pk=pair["companion_port"].pk).update(port_type="1gbe_sfp")
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_succeeds_on_a_second_valid_pair_beyond_production(self) -> None:
        # A second, independently-declared host/companion type pair — the
        # guard/move logic must be generic, not hardcoded to DM7C/DM3.
        NetworkDevicePort = self.apps.get_model("inventory", "NetworkDevicePort")
        types_a = self._make_types()
        types_b = self._make_types()
        pair_a = self._make_device_pair(types_a)
        pair_b = self._make_device_pair(types_b)

        self._run_migration()  # must not raise

        for pair in (pair_a, pair_b):
            moved = NetworkDevicePort.objects.get(pk=pair["companion_port"].pk)
            self.assertEqual(moved.device_id, pair["host"].pk)
            self.assertEqual(moved.description, "Device Control")

    def test_refuses_when_destination_already_has_a_device_control_port(self) -> None:
        apps = self.apps
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        pair = self._make_device_pair(types)
        NetworkDevicePort.objects.create(
            device=pair["host"],
            description="Device Control",
            vlan=types["vlan_dante"],
            port_type="1gbe_rj45",
            address=self._next_address(),
            ordinal=99,
        )
        with self.assertRaises(RuntimeError):
            self._run_migration()

    def test_refuses_when_destination_ordinal_already_taken(self) -> None:
        # The host type has 3 ports (ordinals 1-3), so the migration would
        # assign the new Device Control type port ordinal 4 — pre-occupy
        # that ordinal on the host device with a differently-described
        # port so the description guard above doesn't fire first.
        apps = self.apps
        NetworkDevicePort = apps.get_model("inventory", "NetworkDevicePort")
        types = self._make_types()
        pair = self._make_device_pair(types)
        NetworkDevicePort.objects.create(
            device=pair["host"],
            description="Already Here",
            vlan=types["vlan_dante"],
            port_type="1gbe_rj45",
            address=self._next_address(),
            ordinal=4,
        )
        with self.assertRaises(RuntimeError):
            self._run_migration()


class _FakeSchemaEditor:
    """A stand-in for the real ``SchemaEditor`` migration ``RunPython``
    functions receive — ``0014_retire_companions.retire_companions()``
    only ever reads ``.connection.alias`` off it, never calls a schema
    method, so a real one (which would attempt DDL these tests don't
    want) is unnecessary. A plain ``SimpleNamespace``, not the real
    ``connection`` object — mutating the real connection's ``.alias``
    would corrupt it for every other test.
    """

    def __init__(self, alias: str) -> None:
        self.connection = SimpleNamespace(alias=alias)


class SwitchAddressMaterializationTests(TestCase):
    """ADR 0016: switch creation materializes one NetworkSwitchAddress per
    rack VLAN range (rack-range-base + rack-slot, ``suggest_slot_address()``
    reused as-is — see ``NetworkSwitch._materialize_addresses()``), mirroring
    ADR 0013's device port path.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_b, address_range="10.201.1.0/27")
        self.switch_type = _make_switch_type()

    def test_default_materializes_one_address_per_rack_range(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=2)
        addresses = {a.vlan.name: a.address for a in switch.addresses.select_related("vlan")}
        self.assertEqual(addresses, {"Control": "10.200.1.2", "Dante Primary": "10.201.1.2"})

    def test_manual_choice_materializes_none(self) -> None:
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=2,
            address_materialization=SwitchAddressing.MANUAL,
        )
        self.assertFalse(switch.addresses.exists())

    def test_unracked_switch_materializes_none_under_either_choice(self) -> None:
        for choice in SwitchAddressing.values:
            switch = NetworkSwitch.objects.create(  # type: ignore[misc]
                switch_type=self.switch_type, address_materialization=choice
            )
            self.assertFalse(switch.addresses.exists())

    def test_rack_with_no_ranges_produces_no_addresses_and_no_error(self) -> None:
        empty_rack = Rack.objects.create(name="Empty Rack", slot_count=4)
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=empty_rack, rack_slot=1)
        self.assertFalse(switch.addresses.exists())

    def test_collision_rolls_back_switch_and_addresses_atomically(self) -> None:
        # Occupies slot 2's Dante Primary address ahead of time. vlan_a
        # (200) sorts before vlan_b (201), so materializing a slot-2 switch
        # succeeds on Control first, then collides on Dante Primary —
        # proving the switch *and* the Control address materialized just
        # before the collision both get rolled back, not just that the
        # colliding address itself is refused.
        occupying_switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=3,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=occupying_switch, vlan=self.vlan_b, address="10.201.1.2")
        switch_count_before = NetworkSwitch.objects.count()
        address_count_before = NetworkSwitchAddress.objects.count()
        with self.assertRaises(ValidationError):
            NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=2)
        self.assertEqual(NetworkSwitch.objects.count(), switch_count_before)
        self.assertEqual(NetworkSwitchAddress.objects.count(), address_count_before)

    def test_addresses_materialized_in_vlan_id_order(self) -> None:
        # vlan_high is created (and ranged) first, so it gets the lower pk —
        # but its vlan_id (250) sorts *after* vlan_low's (100). The
        # assertion below can only pass if materialization actually orders
        # by vlan__vlan_id (decision 5) rather than by RackVlanRange
        # creation order or VLAN pk.
        vlan_high = VLAN.objects.create(name="High ID", vlan_id=250, subnet="10.250.0.0/21")
        vlan_low = VLAN.objects.create(name="Low ID", vlan_id=100, subnet="10.100.0.0/21")
        rack = Rack.objects.create(name="Order Rack", slot_count=4)
        RackVlanRange.objects.create(rack=rack, vlan=vlan_high, address_range="10.250.1.0/27")
        RackVlanRange.objects.create(rack=rack, vlan=vlan_low, address_range="10.100.1.0/27")
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, rack=rack, rack_slot=1)
        addresses = list(NetworkSwitchAddress.objects.filter(switch=switch).order_by("pk"))
        self.assertEqual([a.vlan_id for a in addresses], [vlan_low.pk, vlan_high.pk])

    def test_default_address_materialization_is_static(self) -> None:
        switch = NetworkSwitch(switch_type=self.switch_type)
        self.assertEqual(switch.address_materialization, SwitchAddressing.STATIC)

    def test_address_materialization_accepted_as_create_kwarg(self) -> None:
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type, address_materialization=SwitchAddressing.MANUAL
        )
        self.assertEqual(switch.address_materialization, SwitchAddressing.MANUAL)

    def test_invalid_address_materialization_rejected_not_silently_static(self) -> None:
        with self.assertRaises(ValidationError):
            NetworkSwitch.objects.create(  # type: ignore[misc]
                switch_type=self.switch_type, address_materialization="bogus"
            )

    def test_admin_add_post_with_static_materializes_static(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminswitch1", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminswitch1", password="testpass123")

        response = self.client.post(
            "/admin/inventory/networkswitch/add/",
            {
                "switch_type": self.switch_type.pk,
                "hostname": "sw1",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "address_materialization": SwitchAddressing.STATIC,
                "addresses-TOTAL_FORMS": "0",
                "addresses-INITIAL_FORMS": "0",
                "addresses-MIN_NUM_FORMS": "0",
                "addresses-MAX_NUM_FORMS": "1000",
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        switch = NetworkSwitch.objects.get(hostname="sw1")
        addresses = {a.vlan.name: a.address for a in switch.addresses.select_related("vlan")}
        self.assertEqual(addresses, {"Control": "10.200.1.2", "Dante Primary": "10.201.1.2"})

    def test_admin_add_post_address_collision_renders_form_error_not_500(self) -> None:
        """Switch-side twin of ``StaticPortAddressingTests.
        test_admin_add_post_address_collision_renders_form_error_not_500``
        — the whole justification for ADR 0016's clean()-time pre-flight
        departure. Left unconverted, Django's ``add_error()`` raises a raw
        ``ValueError`` for a nonexistent form field instead of rendering a
        validation message — an ordinary Editor creating a switch that
        collides with an existing address would 500, not see a form error.
        """
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminswitch2", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminswitch2", password="testpass123")
        # MANUAL — occupies slot 2's Control address ahead of time so the
        # new switch's own materialization attempt collides on it.
        occupying_switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=3,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=occupying_switch, vlan=self.vlan_a, address="10.200.1.2")

        response = self.client.post(
            "/admin/inventory/networkswitch/add/",
            {
                "switch_type": self.switch_type.pk,
                "hostname": "sw2",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "address_materialization": SwitchAddressing.STATIC,
                "addresses-TOTAL_FORMS": "0",
                "addresses-INITIAL_FORMS": "0",
                "addresses-MIN_NUM_FORMS": "0",
                "addresses-MAX_NUM_FORMS": "1000",
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-renders with a form error, not a 500
        self.assertContains(response, "10.200.1.2")
        self.assertFalse(NetworkSwitch.objects.filter(hostname="sw2").exists())

    def test_admin_add_post_with_inline_address_row_is_silently_ignored(self) -> None:
        """ADR 0016's second departure: ``NetworkSwitchAddressInline.
        has_add_permission()`` returns ``False`` when ``obj is None``.
        Without it, an operator who fills in this inline for a VLAN
        materialization already claims would hit an ``IntegrityError`` on
        ``unique_switch_vlan_address`` once the switch saves (materialization
        runs from ``save_model()``, *before* ``save_related()`` saves this
        inline's formset, and can't see the not-yet-existing inline row to
        avoid the collision by inspection). Blocking the add doesn't error —
        Django's ``BaseInlineFormSet.has_changed()`` treats a submitted new
        row as unchanged when ``has_add_permission`` is false, so it's
        silently dropped and the switch saves normally.
        """
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("adminswitch3", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adminswitch3", password="testpass123")

        response = self.client.post(
            "/admin/inventory/networkswitch/add/",
            {
                "switch_type": self.switch_type.pk,
                "hostname": "sw3",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "2",
                "address_materialization": SwitchAddressing.STATIC,
                "addresses-TOTAL_FORMS": "1",
                "addresses-INITIAL_FORMS": "0",
                "addresses-MIN_NUM_FORMS": "0",
                "addresses-MAX_NUM_FORMS": "1000",
                "addresses-0-vlan": str(self.vlan_a.pk),
                "addresses-0-address": "10.200.1.99",
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        switch = NetworkSwitch.objects.get(hostname="sw3")
        addresses = {a.vlan.name: a.address for a in switch.addresses.select_related("vlan")}
        self.assertEqual(addresses, {"Control": "10.200.1.2", "Dante Primary": "10.201.1.2"})

    def test_admin_add_form_shows_field_preselected_static(self) -> None:
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        request = RequestFactory().get("/admin/inventory/networkswitch/add/")
        form_class = admin.get_form(request, None)
        self.assertIn("address_materialization", form_class.base_fields)
        self.assertEqual(form_class.base_fields["address_materialization"].initial, SwitchAddressing.STATIC)

    def test_admin_change_form_omits_field(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        request = RequestFactory().get(f"/admin/inventory/networkswitch/{switch.pk}/change/")
        form_class = admin.get_form(request, switch)
        self.assertNotIn("address_materialization", form_class.base_fields)

    def test_address_inline_add_blocked_on_add_page(self) -> None:
        inline = NetworkSwitchAddressInline(NetworkSwitch, AdminSite())
        request = RequestFactory().get("/admin/inventory/networkswitch/add/")
        self.assertFalse(inline.has_add_permission(request, None))


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
        # Explicit DHCP: this class is about locked fields, not addressing
        # defaults — test_device_port_can_be_made_static below needs a DHCP
        # starting point to actually exercise the DHCP -> static transition.
        self.device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            port_addressing=PortAddressing.DHCP,
        )
        self.device_port = self.device.ports.get()

    def test_switch_port_type_cannot_change_via_plain_save(self) -> None:
        self.switch_port.port_type = PortType.TEN_GBE_SFP_PLUS
        with self.assertRaises(ValidationError):
            self.switch_port.save()

    def test_switch_port_profile_remains_editable_when_free(self) -> None:
        new_profile = _make_profile(self.vlan_a, name="Audio Trunk")
        self.switch_port.profile = new_profile
        self.switch_port.description = "uplink"
        self.switch_port.save()  # must not raise
        self.switch_port.refresh_from_db()
        self.assertEqual(self.switch_port.profile, new_profile)

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
        # Explicit DHCP so this test actually exercises the DHCP -> static
        # transition it's named for, rather than starting out static already
        # (ADR 0013's new default) and never actually flipping.
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type, rack=self.rack, rack_slot=1, port_addressing=PortAddressing.DHCP
        )
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()
        self.assertEqual(port.default_gateway, "10.200.0.1")

    def test_gateway_follows_later_vlan_gateway_change(self) -> None:
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type, rack=self.rack, rack_slot=1, port_addressing=PortAddressing.DHCP
        )
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()
        self.vlan.default_gateway = "10.200.0.254"
        self.vlan.save()
        port.refresh_from_db()
        self.assertEqual(port.default_gateway, "10.200.0.254")


class VLANRemovalViaProfileTests(TestCase):
    """ADR 0012 / ADR 0007: a VLAN referenced only via a
    ``SwitchPortVlanProfile``'s ``native_vlan`` or ``allowed_vlans`` must
    still block removal — the explicit through model's ``PROTECT`` FK is
    what makes ``allowed_vlans`` hold for both single-object and
    bulk/queryset deletion (a plain M2M's auto-generated join table can't
    protect at all; ADR 0010 established this same guard for the
    switch/device type-port ``allowed_vlans`` this profile's field replaces).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        self.other_vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_vlan_removal_blocked_as_profile_native_vlan(self) -> None:
        _make_profile(self.vlan, name="Media Trunk")
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_by_profile_allowed_vlans(self) -> None:
        profile = _make_profile(self.other_vlan, name="Audio Trunk")
        profile.allowed_vlans.add(self.vlan)
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_vlan_removal_blocked_via_bulk_queryset_delete(self) -> None:
        profile = _make_profile(self.other_vlan, name="Audio Trunk")
        profile.allowed_vlans.add(self.vlan)
        with self.assertRaises(ProtectedError):
            VLAN.objects.filter(pk=self.vlan.pk).delete()


class SwitchPortVlanProfileModelTests(TestCase):
    """Basic ADR 0012 model invariants: identity, defaults, and the
    allowed-VLAN query API.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")

    def test_name_cannot_be_blank(self) -> None:
        profile = SwitchPortVlanProfile(name="", native_vlan=self.vlan_a)
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_db_rejects_blank_name_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            SwitchPortVlanProfile.objects.bulk_create(
                [SwitchPortVlanProfile(name="", native_vlan=self.vlan_a)]
            )

    def test_defaults_to_trunk_mode(self) -> None:
        profile = SwitchPortVlanProfile.objects.create(name="Untouched", native_vlan=self.vlan_a)
        self.assertEqual(profile.port_mode, PortMode.TRUNK)

    def test_str_is_name_only(self) -> None:
        profile = _make_profile(self.vlan_a, name="Audio Trunk")
        self.assertEqual(str(profile), "Audio Trunk")

    def test_effective_allowed_vlans_includes_native(self) -> None:
        profile = _make_profile(self.vlan_a, name="Audio Trunk")
        profile.allowed_vlans.add(self.vlan_b)
        self.assertEqual(profile.effective_allowed_vlans, {self.vlan_a, self.vlan_b})
        self.assertFalse(profile.allows_all_vlans)

    def test_allows_all_vlans_flag(self) -> None:
        profile = SwitchPortVlanProfile.objects.create(
            name="Trunk All", native_vlan=self.vlan_a, all_vlans_allowed=True
        )
        self.assertTrue(profile.allows_all_vlans)


class SwitchPortVlanProfileInvariantTests(TestCase):
    """The three ``allowed_vlans`` invariants (ADR 0012) — native VLAN not
    also listed as allowed; no allowed VLANs while ``all_vlans_allowed`` is
    set; no allowed VLANs in Access mode — tested against each of the four
    enforcement paths named in ``SwitchPortVlanProfile``'s docstring, since
    none of them can live in ``Model.clean()`` alone.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.vlan_c = VLAN.objects.create(name="Dante Secondary", vlan_id=202, subnet="10.202.0.0/21")

    # Path 1: admin form clean() — new (unsaved) profile.
    def test_form_rejects_all_vlans_allowed_with_explicit_allowed_vlans_on_create(self) -> None:
        form = SwitchPortVlanProfileForm(
            data={
                "name": "Bad Trunk",
                "port_mode": PortMode.TRUNK,
                "native_vlan": self.vlan_a.pk,
                "all_vlans_allowed": "on",
                "allowed_vlans": [self.vlan_b.pk],
            }
        )
        self.assertFalse(form.is_valid())

    def test_form_rejects_native_vlan_also_listed_as_allowed(self) -> None:
        form = SwitchPortVlanProfileForm(
            data={
                "name": "Bad Trunk",
                "port_mode": PortMode.TRUNK,
                "native_vlan": self.vlan_a.pk,
                "allowed_vlans": [self.vlan_a.pk],
            }
        )
        self.assertFalse(form.is_valid())

    def test_form_accepts_valid_trunk_with_allowed_vlans(self) -> None:
        form = SwitchPortVlanProfileForm(
            data={
                "name": "Good Trunk",
                "port_mode": PortMode.TRUNK,
                "native_vlan": self.vlan_a.pk,
                "allowed_vlans": [self.vlan_b.pk, self.vlan_c.pk],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    # Path 1, edit: existing profile's M2M selection changes via the form —
    # ModelForm.save_m2m() runs after save(), so clean() alone can't see this.
    def test_form_rejects_flipping_all_vlans_allowed_while_submitting_allowed_vlans(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        profile.allowed_vlans.add(self.vlan_b)
        form = SwitchPortVlanProfileForm(
            data={
                "name": profile.name,
                "port_mode": PortMode.TRUNK,
                "native_vlan": self.vlan_a.pk,
                "all_vlans_allowed": "on",
                "allowed_vlans": [self.vlan_b.pk],
            },
            instance=profile,
        )
        self.assertFalse(form.is_valid())

    # Path 2: the m2m_changed receiver — .add()/.set() never call
    # SwitchPortVlanProfile.save() at all.
    def test_add_rejects_native_vlan_as_allowed(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        with self.assertRaises(ValidationError):
            profile.allowed_vlans.add(self.vlan_a)

    def test_add_rejects_when_all_vlans_allowed(self) -> None:
        profile = SwitchPortVlanProfile.objects.create(
            name="Trunk All", native_vlan=self.vlan_a, all_vlans_allowed=True
        )
        with self.assertRaises(ValidationError):
            profile.allowed_vlans.add(self.vlan_b)

    def test_set_rejects_native_vlan_as_allowed(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        with self.assertRaises(ValidationError):
            profile.allowed_vlans.set([self.vlan_a])

    def test_add_allows_ordinary_non_native_vlan(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        profile.allowed_vlans.add(self.vlan_b)  # must not raise
        self.assertIn(self.vlan_b, profile.allowed_vlans.all())

    # Path 3: the through model's own clean()/save() — direct row creation
    # never fires m2m_changed at all.
    def test_direct_through_row_rejects_native_vlan_via_clean(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        link = SwitchPortVlanProfileAllowedVlan(profile=profile, vlan=self.vlan_a)
        with self.assertRaises(ValidationError):
            link.full_clean()

    def test_direct_through_row_rejects_when_all_vlans_allowed_bypassing_clean(self) -> None:
        profile = SwitchPortVlanProfile.objects.create(
            name="Trunk All", native_vlan=self.vlan_a, all_vlans_allowed=True
        )
        with self.assertRaises(ValidationError):
            SwitchPortVlanProfileAllowedVlan.objects.create(profile=profile, vlan=self.vlan_b)

    # Path 4: the profile's own save() re-checked against already-persisted
    # links — pending (same-submission) M2M changes aren't visible here.
    def test_scalar_flip_rejected_against_persisted_allowed_vlans(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        profile.allowed_vlans.add(self.vlan_b)
        profile.all_vlans_allowed = True
        with self.assertRaises(ValidationError):
            profile.save()

    def test_native_vlan_change_rejected_against_persisted_allowed_vlans(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        profile.allowed_vlans.add(self.vlan_b)
        profile.native_vlan = self.vlan_b
        with self.assertRaises(ValidationError):
            profile.save()

    def test_scalar_flip_allowed_once_allowed_vlans_cleared(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        profile.allowed_vlans.add(self.vlan_b)
        profile.allowed_vlans.clear()
        profile.all_vlans_allowed = True
        profile.save()  # must not raise


class SwitchPortVlanProfileLockTests(TestCase):
    """ADR 0012: ``port_mode``/``native_vlan`` lock once a real
    ``NetworkSwitchPort`` references the profile (not merely a type port);
    ``allowed_vlans``/``all_vlans_allowed``/``name`` stay editable even
    then. The system profile locks all three scalars permanently.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")

    def _make_in_use_profile(self, name: str) -> SwitchPortVlanProfile:
        profile = _make_profile(self.vlan_a, name=name)
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name=f"{name}-type", port_count=1
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        NetworkSwitch.objects.create(switch_type=switch_type)
        return profile

    def test_scalars_editable_while_only_referenced_by_type_port(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        switch_type = _make_switch_type(port_count=0)
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        profile.native_vlan = self.vlan_b
        profile.save()  # must not raise — no real switch port yet
        profile.refresh_from_db()
        self.assertEqual(profile.native_vlan, self.vlan_b)

    def test_native_vlan_locked_once_real_port_exists(self) -> None:
        profile = self._make_in_use_profile("Locked1")
        profile.native_vlan = self.vlan_b
        with self.assertRaises(ValidationError):
            profile.save()

    def test_port_mode_locked_once_real_port_exists(self) -> None:
        profile = self._make_in_use_profile("Locked2")
        profile.port_mode = PortMode.ACCESS
        with self.assertRaises(ValidationError):
            profile.save()

    def test_all_vlans_allowed_editable_on_ordinary_in_use_profile(self) -> None:
        profile = self._make_in_use_profile("Locked3")
        profile.all_vlans_allowed = True
        profile.save()  # must not raise
        profile.refresh_from_db()
        self.assertTrue(profile.all_vlans_allowed)

    def test_name_editable_while_in_use(self) -> None:
        profile = self._make_in_use_profile("Locked4")
        profile.name = "Renamed Trunk"
        profile.save()  # must not raise

    def test_system_profile_all_vlans_allowed_locked(self) -> None:
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        system_profile.all_vlans_allowed = False
        with self.assertRaises(ValidationError):
            system_profile.save()

    def test_system_profile_name_editable(self) -> None:
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        system_profile.name = "Default (Renamed)"
        system_profile.save()  # must not raise

    def test_admin_readonly_fields_empty_for_unused_profile(self) -> None:
        profile = _make_profile(self.vlan_a, name="Unused")
        admin = SwitchPortVlanProfileAdmin(SwitchPortVlanProfile, AdminSite())
        self.assertEqual(admin.get_readonly_fields(RequestFactory().get("/"), profile), [])

    def test_admin_readonly_fields_for_in_use_profile(self) -> None:
        profile = self._make_in_use_profile("Locked5")
        admin = SwitchPortVlanProfileAdmin(SwitchPortVlanProfile, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), profile)
        self.assertEqual(set(readonly), {"native_vlan", "port_mode"})

    def test_admin_readonly_fields_for_system_profile(self) -> None:
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        admin = SwitchPortVlanProfileAdmin(SwitchPortVlanProfile, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), system_profile)
        self.assertEqual(set(readonly), {"native_vlan", "port_mode", "all_vlans_allowed"})


class SwitchPortVlanProfileDeletionTests(TestCase):
    """Bypass paths for the system-profile guard, matching the codebase's
    existing style of proving locked-field enforcement holds without
    ``full_clean()`` (see ``test_db_rejects_zero_rack_slot_bypassing_clean``).
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_is_system_profile_cannot_be_deleted(self) -> None:
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        with self.assertRaises(ValidationError):
            system_profile.delete()

    def test_is_system_cannot_be_cleared_via_plain_save(self) -> None:
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        system_profile.is_system = False
        with self.assertRaises(ValidationError):
            system_profile.save()
        system_profile.refresh_from_db()
        self.assertTrue(system_profile.is_system)

    def test_bulk_queryset_delete_blocked_for_system_profile(self) -> None:
        with self.assertRaises(ValidationError):
            SwitchPortVlanProfile.objects.filter(is_system=True).delete()

    def test_ordinary_unused_profile_can_be_deleted(self) -> None:
        profile = _make_profile(self.vlan_a, name="Throwaway")
        profile.delete()  # must not raise
        self.assertFalse(SwitchPortVlanProfile.objects.filter(pk=profile.pk).exists())

    def test_profile_in_use_by_real_port_cannot_be_deleted(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Del-Test", port_count=1
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        NetworkSwitch.objects.create(switch_type=switch_type)
        with self.assertRaises(ValidationError):
            profile.delete()

    def test_profile_referenced_only_by_type_port_cannot_be_deleted(self) -> None:
        """A profile referenced only by a Type Port (no real switch yet) is
        still fully *editable* (``_in_use()`` is scoped to real ports), but
        it is not *deletable* — ``_referenced_by_any_port()`` checks type
        ports too, so this raises the same friendly ``ValidationError`` the
        real-port case does, rather than falling through to a raw
        ``ProtectedError`` from ``NetworkSwitchTypePort.profile``.
        """
        profile = _make_profile(self.vlan_a, name="Trunk")
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Del-Test-TypeOnly", port_count=0
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        with self.assertRaises(ValidationError):
            profile.delete()

    def test_bulk_queryset_delete_blocked_by_type_port_only(self) -> None:
        profile = _make_profile(self.vlan_a, name="Trunk")
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name="Del-Test-Bulk", port_count=0
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        with self.assertRaises(ValidationError):
            SwitchPortVlanProfile.objects.filter(pk=profile.pk).delete()


class SwitchPortProfileConnectedLockTests(TestCase):
    """DESIGN.md: a switch port's profile can be swapped for another
    "unless a device is already connected" — and
    ``InlineModelAdmin.get_readonly_fields()`` can't express that per-row
    (it receives the parent switch, not each port), so this is enforced by
    ``NetworkSwitchPortForm`` disabling the field per row instead.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan_a, address_range="10.200.1.0/27")
        self.switch_type = _make_switch_type(port_count=2)
        self.switch = NetworkSwitch.objects.create(switch_type=self.switch_type)
        self.connected_port, self.free_port = self.switch.ports.order_by("port_number")
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan_a)
        # Explicit DHCP: this class is about switch-port profile locking,
        # not device addressing — the choice here is incidental.
        self.device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            port_addressing=PortAddressing.DHCP,
        )
        device_port = self.device.ports.get()
        device_port.switch_port = self.connected_port
        device_port.save()
        self.other_profile = _make_profile(self.vlan_b, name="Other")

    def test_profile_change_rejected_on_connected_port(self) -> None:
        self.connected_port.profile = self.other_profile
        with self.assertRaises(ValidationError):
            self.connected_port.save()

    def test_profile_change_allowed_on_free_port(self) -> None:
        self.free_port.profile = self.other_profile
        self.free_port.save()  # must not raise
        self.free_port.refresh_from_db()
        self.assertEqual(self.free_port.profile, self.other_profile)

    def test_profile_change_allowed_after_disconnecting(self) -> None:
        device_port = NetworkDevicePort.objects.get(switch_port=self.connected_port)
        device_port.switch_port = None
        device_port.save()

        self.connected_port.profile = self.other_profile
        self.connected_port.save()  # must not raise

    def test_mixed_inline_formset_disables_only_connected_row(self) -> None:
        FormSet = inlineformset_factory(
            NetworkSwitch,
            NetworkSwitchPort,
            form=NetworkSwitchPortForm,
            fields=["port_number", "description", "port_type", "profile"],
            extra=0,
        )
        formset = FormSet(instance=self.switch)
        forms_by_pk = {form.instance.pk: form for form in formset.forms}
        self.assertTrue(forms_by_pk[self.connected_port.pk].fields["profile"].disabled)
        self.assertFalse(forms_by_pk[self.free_port.pk].fields["profile"].disabled)


class L2OnlyVlanTests(TestCase):
    """ADR 0012: a VLAN with a blank subnet is L2-only — usable as a
    profile's native/allowed VLAN or a device type port's VLAN (DHCP only),
    but not addressable in any way. Gateway/DHCP is a DB CheckConstraint;
    the cross-table rules (RackVlanRange, switch address, static device
    port) are model-validation guarantees only, same
    QuerySet.update()/bulk_create() limitations as everywhere else here.
    """

    def setUp(self) -> None:
        self.l2_vlan = VLAN.objects.create(name="L2 Only", vlan_id=999, subnet="")

    def test_usable_as_profile_native_vlan(self) -> None:
        profile = _make_profile(self.l2_vlan, name="L2 Trunk")
        self.assertEqual(profile.native_vlan, self.l2_vlan)

    def test_usable_as_profile_allowed_vlan(self) -> None:
        other = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        profile = _make_profile(other, name="Trunk")
        profile.allowed_vlans.add(self.l2_vlan)  # must not raise
        self.assertIn(self.l2_vlan, profile.allowed_vlans.all())

    def test_usable_as_device_type_port_vlan(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.l2_vlan)
        device = NetworkDevice.objects.create(device_type=device_type)
        port = device.ports.get()
        self.assertTrue(port.is_dhcp)
        self.assertEqual(port.vlan, self.l2_vlan)

    def test_gateway_rejected_via_full_clean(self) -> None:
        self.l2_vlan.default_gateway = "10.0.0.1"
        with self.assertRaises(ValidationError):
            self.l2_vlan.full_clean()

    def test_dhcp_range_rejected_via_full_clean(self) -> None:
        self.l2_vlan.dhcp_range_start = "10.0.0.2"
        self.l2_vlan.dhcp_range_end = "10.0.0.10"
        with self.assertRaises(ValidationError):
            self.l2_vlan.full_clean()

    def test_db_rejects_gateway_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            VLAN.objects.filter(pk=self.l2_vlan.pk).update(default_gateway="10.0.0.1")

    def test_db_rejects_dhcp_range_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            VLAN.objects.filter(pk=self.l2_vlan.pk).update(
                dhcp_range_start="10.0.0.2", dhcp_range_end="10.0.0.10"
            )

    def test_rack_vlan_range_rejected(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        range_ = RackVlanRange(rack=rack, vlan=self.l2_vlan, address_range="10.0.0.0/27")
        with self.assertRaises(ValidationError):
            range_.full_clean()

    def test_switch_address_rejected(self) -> None:
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        switch.rack = rack
        switch.rack_slot = 1
        switch.save()
        address = NetworkSwitchAddress(switch=switch, vlan=self.l2_vlan, address="10.0.0.1")
        with self.assertRaises(ValidationError):
            address.full_clean()

    def test_static_device_port_rejected(self) -> None:
        device_type = _make_device_type(port_count=1, vlan=self.l2_vlan)
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        device = NetworkDevice.objects.create(device_type=device_type, rack=rack, rack_slot=1)
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.0.0.1"
        with self.assertRaises(ValidationError):
            port.full_clean()


class SeedDefaultsTests(TestCase):
    """ADR 0012: the migration-seeded system Default VLAN/profile, and the
    ``seed_defaults`` management command that can re-seed them if removed
    (e.g. by ``manage.py flush``, which the migration can't repair after
    the fact).
    """

    def test_default_vlan_and_profile_seeded_by_migration(self) -> None:
        vlan = VLAN.objects.get(vlan_id=DEFAULT_VLAN_ID)
        self.assertEqual(vlan.name, DEFAULT_VLAN_NAME)
        self.assertEqual(vlan.subnet, "")
        profile = SwitchPortVlanProfile.objects.get(is_system=True)
        self.assertEqual(profile.name, DEFAULT_PROFILE_NAME)
        self.assertEqual(profile.native_vlan, vlan)
        self.assertTrue(profile.all_vlans_allowed)
        self.assertEqual(profile.port_mode, PortMode.TRUNK)

    def test_seed_migration_raises_on_conflicting_pre_existing_vlan(self) -> None:
        import importlib

        from django.apps import apps as real_apps

        seed_module = importlib.import_module("inventory.migrations.0006_switch_port_vlan_profiles")
        vlan = VLAN.objects.get(vlan_id=DEFAULT_VLAN_ID)
        vlan.name = "Something Else"
        vlan.save()
        with self.assertRaises(RuntimeError):
            seed_module.seed_defaults(real_apps, None)

    def test_seed_migration_raises_on_conflicting_pre_existing_profile(self) -> None:
        import importlib

        from django.apps import apps as real_apps

        seed_module = importlib.import_module("inventory.migrations.0006_switch_port_vlan_profiles")
        profile = SwitchPortVlanProfile.objects.get(is_system=True)
        SwitchPortVlanProfile.objects.filter(pk=profile.pk).update(is_system=False)
        with self.assertRaises(RuntimeError):
            seed_module.seed_defaults(real_apps, None)

    def test_migration_data_steps_reverse_as_documented_noops(self) -> None:
        """Both RunPython steps — seeding the system rows, and backfilling
        the profile FK onto historical rows — are documented no-ops on
        reverse (see their docstrings for why: neither can distinguish a
        row/value it created from one it merely found or left alone).
        """
        import importlib

        from django.db import migrations as django_migrations

        seed_module = importlib.import_module("inventory.migrations.0006_switch_port_vlan_profiles")
        run_python_ops = [
            op for op in seed_module.Migration.operations if isinstance(op, django_migrations.RunPython)
        ]
        self.assertEqual(len(run_python_ops), 2)
        for op in run_python_ops:
            self.assertIs(op.reverse_code, django_migrations.RunPython.noop)

    def test_seed_defaults_command_is_idempotent(self) -> None:
        call_command("seed_defaults")
        call_command("seed_defaults")  # must not raise
        self.assertEqual(VLAN.objects.filter(vlan_id=DEFAULT_VLAN_ID).count(), 1)
        self.assertEqual(SwitchPortVlanProfile.objects.filter(is_system=True).count(), 1)

    def test_seed_defaults_command_restores_rows_after_removal(self) -> None:
        # Simulates the manage.py flush gap the command exists to close —
        # QuerySet.update()/bulk-bypass paths are the same documented
        # limitation used throughout this file to reach an otherwise
        # guarded state directly.
        SwitchPortVlanProfile.objects.filter(is_system=True).update(is_system=False)
        SwitchPortVlanProfile.objects.filter(name=DEFAULT_PROFILE_NAME).delete()
        VLAN.objects.filter(vlan_id=DEFAULT_VLAN_ID).delete()

        call_command("seed_defaults")

        self.assertTrue(VLAN.objects.filter(vlan_id=DEFAULT_VLAN_ID).exists())
        self.assertTrue(SwitchPortVlanProfile.objects.filter(is_system=True).exists())


class ReviewCouncilRegressionTests(TestCase):
    """Regressions found by the review council (see the workspace report).

    Each test here corresponds to a defect that shipped in the first draft of
    ADR 0012's implementation and was caught by an independent reviewer.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)

    # --- P0: seed_defaults keyed on `name`, which is editable -------------
    def test_seed_defaults_finds_renamed_system_profile_instead_of_duplicating(self) -> None:
        """Renaming the system profile is supported (``name`` is never
        locked). ``seed_defaults`` runs on every container start, so keying
        it on ``name`` meant one rename produced a *second* is_system row —
        after which ``default_switch_port_vlan_profile()`` raised
        MultipleObjectsReturned on every switch-port creation, unrecoverably
        (neither row can be deleted through the app).
        """
        system_profile = SwitchPortVlanProfile.objects.get(is_system=True)
        system_profile.name = "Default Trunk"
        system_profile.save()

        call_command("seed_defaults")

        self.assertEqual(SwitchPortVlanProfile.objects.filter(is_system=True).count(), 1)
        self.assertEqual(default_switch_port_vlan_profile(), system_profile.pk)

    def test_seed_defaults_errors_rather_than_crashing_when_default_name_is_taken(self) -> None:
        SwitchPortVlanProfile.objects.filter(is_system=True).update(is_system=False)
        SwitchPortVlanProfile.objects.filter(name=DEFAULT_PROFILE_NAME).update(name="Squatted")
        SwitchPortVlanProfile.objects.create(name=DEFAULT_PROFILE_NAME, native_vlan=self.vlan)
        with self.assertRaises(CommandError):
            call_command("seed_defaults")

    def test_seed_defaults_refuses_to_wire_new_profile_to_mismatched_vlan(self) -> None:
        """A previous version of this command warned about a mismatched
        VLAN id=1 but still used it as the new profile's native_vlan —
        silently wiring the system profile to whatever that row actually
        was. Once no system profile exists yet, a mismatch must block
        creation instead, matching the migration's own posture.
        """
        SwitchPortVlanProfile.objects.filter(is_system=True).update(is_system=False)
        SwitchPortVlanProfile.objects.filter(name=DEFAULT_PROFILE_NAME).delete()
        VLAN.objects.filter(vlan_id=DEFAULT_VLAN_ID).update(name="Repurposed", subnet="10.250.0.0/21")
        with self.assertRaises(CommandError):
            call_command("seed_defaults")
        self.assertFalse(SwitchPortVlanProfile.objects.filter(is_system=True).exists())

    def test_seed_defaults_tolerates_vlan_drift_once_profile_exists(self) -> None:
        """Unlike the profile's `is_system` fields, VLAN 1's name/subnet
        aren't documented as permanently locked — renaming it later is a
        legitimate administrative action, not a conflict, once the system
        profile is already wired to it.
        """
        VLAN.objects.filter(vlan_id=DEFAULT_VLAN_ID).update(name="Renamed VLAN 1")
        call_command("seed_defaults")  # must not raise
        self.assertTrue(SwitchPortVlanProfile.objects.filter(is_system=True).exists())

    # --- P1: clearing a VLAN's subnet orphaned its addressing -------------
    def test_clearing_subnet_blocked_by_existing_rack_range(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.vlan.subnet = ""
        self.vlan.default_gateway = None
        with self.assertRaises(ValidationError):
            self.vlan.full_clean()

    def test_clearing_subnet_blocked_by_existing_switch_address(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        # MANUAL — the default STATIC choice would already materialize this
        # VLAN's address at creation (ADR 0016), colliding with the explicit
        # create() below on unique_switch_vlan_address.
        switch = NetworkSwitch.objects.create(  # type: ignore[misc]
            switch_type=_make_switch_type(),
            rack=self.rack,
            rack_slot=1,
            address_materialization=SwitchAddressing.MANUAL,
        )
        NetworkSwitchAddress.objects.create(switch=switch, vlan=self.vlan, address="10.200.1.1")
        self.vlan.subnet = ""
        self.vlan.default_gateway = None
        with self.assertRaises(ValidationError):
            self.vlan.full_clean()

    def test_clearing_subnet_blocked_by_existing_static_device_port(self) -> None:
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        device = NetworkDevice.objects.create(
            device_type=_make_device_type(port_count=1, vlan=self.vlan), rack=self.rack, rack_slot=1
        )
        port = device.ports.get()
        port.is_dhcp = False
        port.address = "10.200.1.1"
        port.save()
        self.vlan.subnet = ""
        self.vlan.default_gateway = None
        with self.assertRaises(ValidationError):
            self.vlan.full_clean()

    def test_clearing_subnet_allowed_when_nothing_is_addressed(self) -> None:
        self.vlan.subnet = ""
        self.vlan.default_gateway = None
        self.vlan.full_clean()  # must not raise
        self.vlan.save()
        self.vlan.refresh_from_db()
        self.assertEqual(self.vlan.subnet, "")

    # --- P1: m2m_changed receiver validated stale in-memory state ---------
    def test_m2m_receiver_reads_committed_state_not_stale_instance(self) -> None:
        """The receiver takes a row lock, so it must validate against what's
        actually persisted. Previously it read the caller's in-memory copy,
        leaving the exact race the lock exists to close wide open.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        stale = SwitchPortVlanProfile.objects.get(pk=profile.pk)  # snapshot before the change

        # Simulate a concurrent, already-committed change the stale copy can't see.
        SwitchPortVlanProfile.objects.filter(pk=profile.pk).update(native_vlan=other_vlan)

        with self.assertRaises(ValidationError):
            stale.allowed_vlans.add(other_vlan)

    def test_m2m_receiver_reads_committed_all_vlans_allowed(self) -> None:
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        stale = SwitchPortVlanProfile.objects.get(pk=profile.pk)
        SwitchPortVlanProfile.objects.filter(pk=profile.pk).update(all_vlans_allowed=True)
        with self.assertRaises(ValidationError):
            stale.allowed_vlans.add(other_vlan)

    # --- P1: two N+1s -----------------------------------------------------
    def test_materialization_does_not_query_profile_per_port(self) -> None:
        switch_type = _make_switch_type(port_count=12)
        with CaptureQueriesContext(connection) as ctx:
            NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw12")
        profile_selects = [
            q["sql"]
            for q in ctx.captured_queries
            if "switchportvlanprofile" in q["sql"].lower() and q["sql"].strip().upper().startswith("SELECT")
        ]
        # One lock per port is expected (each port's save() locks its profile);
        # a second SELECT per port for the profile *object* is the N+1.
        self.assertLessEqual(
            len(profile_selects), 12, f"profile SELECTs should be ~1/port, got {len(profile_selects)}"
        )

    def test_switch_port_inline_does_not_query_connected_port_per_row(self) -> None:
        switch_type = _make_switch_type(port_count=12)
        switch = NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw12")
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(username="np1", password="x", email="a@b.c")
        inline = NetworkSwitchPortInline(NetworkSwitch, AdminSite())
        ports = list(inline.get_queryset(request).filter(switch=switch))
        with CaptureQueriesContext(connection) as ctx:
            for port in ports:
                NetworkSwitchPortForm(instance=port)
        device_port_queries = [
            q["sql"] for q in ctx.captured_queries if "networkdeviceport" in q["sql"].lower()
        ]
        self.assertEqual(
            device_port_queries,
            [],
            "connected_device_port must come from select_related, not one query per inline row",
        )

    # --- P2: through-model save() trusted a stale cached profile object ---
    def test_through_row_save_rejects_against_committed_all_vlans_allowed(self) -> None:
        """``SwitchPortVlanProfileAllowedVlan.save()`` used to validate a
        cached ``self.profile`` FK object rather than the current database
        row. No true concurrency/threading is needed to demonstrate this —
        the bug was that a Python-level object reference held from an
        earlier, unrelated read doesn't reflect a plain, ordinary ``.save()``
        made by someone else in between, not a bypass or a race requiring
        overlapping transactions.
        """
        profile = _make_profile(self.vlan, name="Trunk")
        stale_profile = SwitchPortVlanProfile.objects.get(pk=profile.pk)  # loaded before the change

        SwitchPortVlanProfile.objects.filter(pk=profile.pk).update(all_vlans_allowed=True)

        link = SwitchPortVlanProfileAllowedVlan(profile=stale_profile, vlan=self.vlan)
        with self.assertRaises(ValidationError):
            link.save()

    def test_through_row_save_rejects_against_committed_native_vlan_change(self) -> None:
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        stale_profile = SwitchPortVlanProfile.objects.get(pk=profile.pk)

        SwitchPortVlanProfile.objects.filter(pk=profile.pk).update(native_vlan=other_vlan)

        link = SwitchPortVlanProfileAllowedVlan(profile=stale_profile, vlan=other_vlan)
        with self.assertRaises(ValidationError):
            link.save()

    # --- P3: persisted-links / connected-port checks ignored update_fields
    def test_persisted_links_check_ignores_unrelated_update_fields(self) -> None:
        """``profile.all_vlans_allowed = True`` in memory must not block a
        ``save(update_fields=[...])`` that never actually writes that field.
        """
        profile = _make_profile(self.vlan, name="Trunk")
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile.allowed_vlans.add(other_vlan)

        profile.all_vlans_allowed = True  # in memory only
        profile.name = "Renamed Trunk"
        profile.save(update_fields=["name"])  # must not raise — all_vlans_allowed isn't written

        profile.refresh_from_db()
        self.assertEqual(profile.name, "Renamed Trunk")
        self.assertFalse(profile.all_vlans_allowed)

    def test_persisted_links_check_still_fires_when_field_is_included(self) -> None:
        profile = _make_profile(self.vlan, name="Trunk")
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile.allowed_vlans.add(other_vlan)

        profile.all_vlans_allowed = True
        with self.assertRaises(ValidationError):
            profile.save(update_fields=["all_vlans_allowed"])

    def test_switch_port_profile_guard_ignores_unrelated_update_fields(self) -> None:
        """An in-memory ``profile_id`` that differs from what's persisted
        must not block a ``save(update_fields=[...])`` that never writes
        ``profile`` — even on a port with a connected device.
        """
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=rack, vlan=self.vlan, address_range="10.200.1.0/27")
        switch_type = _make_switch_type(port_count=1)
        switch = NetworkSwitch.objects.create(switch_type=switch_type)
        port = switch.ports.get()
        device_type = _make_device_type(port_count=1, vlan=self.vlan)
        # Explicit DHCP: this test is about the profile guard's
        # update_fields handling, not device addressing.
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type, rack=rack, rack_slot=1, port_addressing=PortAddressing.DHCP
        )
        device_port = device.ports.get()
        device_port.switch_port = port
        device_port.save()

        other_profile = _make_profile(self.vlan, name="Other")
        port.profile_id = other_profile.pk  # in memory only
        port.description = "renamed"
        port.save(update_fields=["description"])  # must not raise — profile isn't written

        port.refresh_from_db()
        self.assertEqual(port.description, "renamed")
        self.assertNotEqual(port.profile_id, other_profile.pk)

    # --- P2: admin form clean() was inert on locked profiles ---------------
    def _make_in_use_profile(self, name: str, **kwargs) -> SwitchPortVlanProfile:
        profile = _make_profile(self.vlan, name=name, **kwargs)
        switch_type = NetworkSwitchType.objects.create(
            manufacturer="Cisco", model="SG300", name=f"{name}-type", port_count=1
        )
        NetworkSwitchTypePort.objects.create(
            switch_type=switch_type, port_number=1, port_type=PortType.GBE_RJ45, profile=profile
        )
        NetworkSwitch.objects.create(switch_type=switch_type)
        return profile

    def _admin_form_for(self, profile: SwitchPortVlanProfile, data: dict):
        """Builds the form the *admin* would actually build for ``profile``
        — critically, going through ``SwitchPortVlanProfileAdmin.get_form()``
        rather than instantiating ``SwitchPortVlanProfileForm`` directly.
        Only ``ModelAdmin.get_form()`` applies ``exclude=readonly_fields``,
        which is what actually drops ``port_mode``/``native_vlan`` from the
        form for a locked profile — instantiating the form class directly
        would require submitting them anyway (they're declared required on
        the class itself) and so would never exercise the fallback this
        test is checking.
        """
        request = RequestFactory().post(f"/admin/inventory/switchportvlanprofile/{profile.pk}/change/")
        request.user = User.objects.create_superuser(
            username=f"formtest-{profile.pk}", password="x", email="a@b.c"
        )
        admin = SwitchPortVlanProfileAdmin(SwitchPortVlanProfile, AdminSite())
        form_class = admin.get_form(request, profile)
        return form_class(data=data, instance=profile)

    def test_form_rejects_all_vlans_allowed_on_locked_access_profile(self) -> None:
        """``port_mode`` is excluded from the form entirely once a profile
        is in use (it's in ``get_readonly_fields()``), so it used to read
        as ``None`` in ``cleaned_data`` — silently passing an Access-mode,
        in-use profile through with ``all_vlans_allowed=True`` submitted.
        The form must fall back to the instance's persisted ``port_mode``.
        """
        profile = self._make_in_use_profile("LockedAccess", port_mode=PortMode.ACCESS)
        form = self._admin_form_for(
            profile, data={"name": profile.name, "all_vlans_allowed": "on", "allowed_vlans": []}
        )
        self.assertFalse(form.is_valid())
        self.assertIn("all_vlans_allowed cannot be set while port_mode is Access", str(form.errors))

    def test_form_still_valid_for_locked_trunk_profile_with_ordinary_edit(self) -> None:
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = self._make_in_use_profile("LockedTrunk")
        form = self._admin_form_for(
            profile, data={"name": "Renamed", "all_vlans_allowed": "", "allowed_vlans": [other_vlan.pk]}
        )
        self.assertTrue(form.is_valid(), form.errors)

    # --- P2: single-submission scalar+M2M edit was rejected -----------------
    def test_form_allows_enabling_all_vlans_allowed_while_clearing_allowed_vlans(self) -> None:
        """Reproduces the ergonomics bug: a valid combined edit (flip
        all_vlans_allowed on, clear the now-incompatible allowed_vlans) used
        to be rejected because the model's persisted-links check runs
        during _post_clean(), before save_m2m() has applied the cleared
        selection — forcing two separate saves for what should be one.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        profile.allowed_vlans.add(other_vlan)

        form = SwitchPortVlanProfileForm(
            data={
                "name": profile.name,
                "port_mode": PortMode.TRUNK,
                "native_vlan": self.vlan.pk,
                "all_vlans_allowed": "on",
                "allowed_vlans": [],
            },
            instance=profile,
        )
        self.assertTrue(form.is_valid(), form.errors)
        saved = form.save(commit=True)  # commit=True already applies save_m2m() internally

        saved.refresh_from_db()
        self.assertTrue(saved.all_vlans_allowed)
        self.assertEqual(list(saved.allowed_vlans.all()), [])

    def test_trust_flag_does_not_bypass_a_genuinely_inconsistent_direct_save(self) -> None:
        """The form-granted exemption is instance-scoped, not a general
        bypass — a plain, non-form save that flips a scalar without ever
        clearing the conflicting persisted links must still be rejected.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        profile.allowed_vlans.add(other_vlan)
        profile.all_vlans_allowed = True
        with self.assertRaises(ValidationError):
            profile.save()

    # --- P2: Access + all_vlans_allowed with an empty VLAN list -------------
    def test_form_rejects_access_mode_with_all_vlans_allowed_on_create(self) -> None:
        form = SwitchPortVlanProfileForm(
            data={
                "name": "Bad Access",
                "port_mode": PortMode.ACCESS,
                "native_vlan": self.vlan.pk,
                "all_vlans_allowed": "on",
                "allowed_vlans": [],
            }
        )
        self.assertFalse(form.is_valid())

    def test_model_rejects_access_mode_with_all_vlans_allowed_via_clean(self) -> None:
        profile = SwitchPortVlanProfile(
            name="Bad Access", native_vlan=self.vlan, port_mode=PortMode.ACCESS, all_vlans_allowed=True
        )
        with self.assertRaises(ValidationError):
            profile.full_clean()

    def test_db_rejects_access_mode_with_all_vlans_allowed_bypassing_clean(self) -> None:
        with self.assertRaises(IntegrityError), transaction.atomic():
            SwitchPortVlanProfile.objects.bulk_create(
                [
                    SwitchPortVlanProfile(
                        name="Bad Access Bulk",
                        native_vlan=self.vlan,
                        port_mode=PortMode.ACCESS,
                        all_vlans_allowed=True,
                    )
                ]
            )

    def test_trunk_mode_with_all_vlans_allowed_remains_valid(self) -> None:
        profile = SwitchPortVlanProfile(
            name="Good Trunk All", native_vlan=self.vlan, port_mode=PortMode.TRUNK, all_vlans_allowed=True
        )
        profile.full_clean()  # must not raise
        profile.save()

    # --- P2: direct through-row writes left no audit entry ------------------
    def test_direct_through_row_creation_is_logged(self) -> None:
        """``m2m_fields`` on the profile's auditlog registration only
        tracks ``.add()``/``.set()``/``.remove()`` (which fire
        ``m2m_changed``) — direct ``SwitchPortVlanProfileAllowedVlan``
        creation is a separately supported write path (it has its own
        ``clean()``/``save()`` validation) that never fires that signal,
        so it needs its own registration to be logged at all.
        """
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")

        link = SwitchPortVlanProfileAllowedVlan.objects.create(profile=profile, vlan=other_vlan)

        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(link.pk), action=LogEntry.Action.CREATE).exists()
        )

    def test_direct_through_row_deletion_is_logged(self) -> None:
        other_vlan = VLAN.objects.create(name="Media", vlan_id=201, subnet="10.201.0.0/21")
        profile = _make_profile(self.vlan, name="Trunk")
        link = SwitchPortVlanProfileAllowedVlan.objects.create(profile=profile, vlan=other_vlan)
        pk = link.pk

        link.delete()

        self.assertTrue(LogEntry.objects.filter(object_pk=str(pk), action=LogEntry.Action.DELETE).exists())


class DepartmentModelTests(TestCase):
    """ADR 0021: Department is a table, optional on VLAN, PROTECT-guarded,
    trimmed the same way RackTemplate is, and never system-seeded.
    """

    def test_vlan_saves_with_no_department(self) -> None:
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.assertIsNone(vlan.department)

    def test_protect_refuses_delete_of_department_with_vlans(self) -> None:
        department = Department.objects.create(name="Audio")
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21", department=department)
        with self.assertRaises(ProtectedError) as ctx:
            department.delete()
        self.assertIn(vlan, ctx.exception.protected_objects)
        self.assertTrue(Department.objects.filter(pk=department.pk).exists())

    def test_delete_succeeds_for_department_with_no_vlans(self) -> None:
        department = Department.objects.create(name="Audio")
        department.delete()
        self.assertFalse(Department.objects.filter(name="Audio").exists())

    def test_name_is_stripped_on_save(self) -> None:
        department = Department.objects.create(name="  Audio  ")
        self.assertEqual(department.name, "Audio")

    def test_name_is_stripped_by_full_clean(self) -> None:
        department = Department(name="  Audio  ")
        department.full_clean()
        self.assertEqual(department.name, "Audio")

    def test_case_variant_duplicate_rejected(self) -> None:
        Department.objects.create(name="Audio")
        with self.assertRaises(IntegrityError):
            Department.objects.create(name="audio")

    def test_blank_name_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            Department.objects.create(name="")

    def test_str_returns_name(self) -> None:
        department = Department.objects.create(name="Audio")
        self.assertEqual(str(department), "Audio")

    def test_no_department_exists_on_fresh_database_and_seed_defaults_creates_none(self) -> None:
        # A decision nothing tests is an intention (ADR 0021 review note 8)
        # — this is what makes "no system-seeded departments" a tested
        # claim rather than an untested one. An accidental migration seed
        # would otherwise pass every other test in this class.
        call_command("seed_defaults")
        self.assertEqual(Department.objects.count(), 0)


class DepartmentAuditTests(TestCase):
    """ADR 0021: audit coverage for the new model and, more importantly,
    for the VLAN side — proof that VLAN's existing bare registration in
    ``AUDITLOG_INCLUDE_TRACKING_MODELS`` picks up the new ``department``
    field with no settings change (review note 6 — the Department-side
    tests alone don't demonstrate this).
    """

    def setUp(self) -> None:
        self.user = User.objects.create_user(username="editor", password="x")
        self.factory = RequestFactory()

    def _request(self):
        request = self.factory.post("/admin/inventory/department/add/")
        request.user = self.user
        return request

    def test_save_model_sets_created_by_on_creation(self) -> None:
        # Follows AuditedModelAdminTests (tests.py:371), not
        # RackTemplateAuditTests — that class demonstrates mutation
        # logging, not created_by stamping (review note 6).
        admin = DepartmentAdmin(Department, AdminSite())
        obj = Department(name="Audio")
        admin.save_model(self._request(), obj, form=None, change=False)
        self.assertEqual(obj.created_by, self.user)

    def test_department_edit_produces_update_log_entry(self) -> None:
        department = Department.objects.create(name="Audio")
        LogEntry.objects.filter(object_pk=str(department.pk)).delete()
        department.description = "Front-of-house audio equipment"
        department.save()
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(department.pk), action=LogEntry.Action.UPDATE).exists()
        )

    def test_vlan_department_assignment_produces_update_log_entry_naming_department(self) -> None:
        department = Department.objects.create(name="Audio")
        vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        LogEntry.objects.filter(object_pk=str(vlan.pk)).delete()
        vlan.department = department
        vlan.save()
        entries = LogEntry.objects.filter(object_pk=str(vlan.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entries.count(), 1)
        self.assertIn("department", entries.first().changes_dict)


class OwnerModelTests(TestCase):
    """ADR 0023 decision 1 / PLAN-hostname-ingredients.md settled decision 4:
    ``Owner`` is a slug+name table, both required and unique, ``slug``
    DNS-validated and lowercased (unlike ``Department.name``, which strips
    but deliberately doesn't casefold).
    """

    def test_slug_is_stripped_and_lowercased_on_save(self) -> None:
        owner = Owner.objects.create(slug="MPS ", name="MPS")
        self.assertEqual(owner.slug, "mps")

    def test_slug_is_stripped_and_lowercased_by_full_clean(self) -> None:
        owner = Owner(slug="MPS ", name="MPS")
        owner.full_clean()
        self.assertEqual(owner.slug, "mps")

    def test_name_is_stripped_but_not_lowercased_on_save(self) -> None:
        owner = Owner.objects.create(slug="mps", name="  MPS  ")
        self.assertEqual(owner.name, "MPS")

    def test_leading_hyphen_slug_rejected(self) -> None:
        owner = Owner(slug="-mps", name="MPS")
        with self.assertRaises(ValidationError):
            owner.full_clean()

    def test_trailing_hyphen_slug_rejected(self) -> None:
        owner = Owner(slug="mps-", name="MPS")
        with self.assertRaises(ValidationError):
            owner.full_clean()

    def test_blank_slug_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            Owner.objects.create(slug="", name="MPS")

    def test_blank_name_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            Owner.objects.create(slug="mps", name="")

    def test_str_returns_name_and_slug(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        self.assertEqual(str(owner), "MPS (mps)")

    def test_protect_refuses_delete_of_owner_with_a_rack(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        rack = Rack.objects.create(name="Rack 1", slot_count=4, owner=owner)
        with self.assertRaises(ProtectedError) as ctx:
            owner.delete()
        self.assertIn(rack, ctx.exception.protected_objects)
        self.assertTrue(Owner.objects.filter(pk=owner.pk).exists())

    def test_protect_refuses_delete_of_owner_with_a_switch(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw1", owner=owner)
        with self.assertRaises(ProtectedError) as ctx:
            owner.delete()
        self.assertIn(switch, ctx.exception.protected_objects)

    def test_protect_refuses_delete_of_owner_with_a_device(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type, hostname="dev1", owner=owner)
        with self.assertRaises(ProtectedError) as ctx:
            owner.delete()
        self.assertIn(device, ctx.exception.protected_objects)


class RackLocationSlugTests(TestCase):
    """ADR 0023 decision 5 / settled decision 5: ``null=True`` +
    ``unique=True``, not a conditional ``UniqueConstraint`` — this backend
    (``supports_partial_indexes = False``) compiles that to no SQL at all.
    """

    def test_blank_string_stores_none_on_save(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4, location_slug="")
        self.assertIsNone(rack.location_slug)

    def test_whitespace_only_stores_none_on_save(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4, location_slug="   ")
        self.assertIsNone(rack.location_slug)

    def test_stripped_and_lowercased_on_save(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4, location_slug=" WPCSRL ")
        self.assertEqual(rack.location_slug, "wpcsrl")

    def test_stripped_and_lowercased_by_full_clean(self) -> None:
        rack = Rack(name="Rack 1", slot_count=4, location_slug=" WPCSRL ")
        rack.full_clean()
        self.assertEqual(rack.location_slug, "wpcsrl")

    def test_two_racks_may_both_be_blank(self) -> None:
        Rack.objects.create(name="Rack 1", slot_count=4, location_slug="")
        Rack.objects.create(name="Rack 2", slot_count=4, location_slug="")  # must not raise

    def test_two_racks_may_not_share_a_location_slug_integrity_error(self) -> None:
        # Asserted at the database level, not just ValidationError — settled
        # decision 5 exists precisely because a partial index would have
        # silently emitted no SQL and left this unenforced.
        Rack.objects.create(name="Rack 1", slot_count=4, location_slug="wpcsrl")
        with self.assertRaises(IntegrityError):
            Rack.objects.create(name="Rack 2", slot_count=4, location_slug="wpcsrl")

    def test_invalid_slug_rejected_by_full_clean(self) -> None:
        rack = Rack(name="Rack 1", slot_count=4, location_slug="-bad")
        with self.assertRaises(ValidationError):
            rack.full_clean()


class TypeHostnameSlugTests(TestCase):
    """ADR 0023 / settled decision 3: ``hostname_slug`` normalises, is not
    unique across two profiles of one model, and stays editable after
    instances exist — deliberately outside the ADR 0010 profile lock.
    """

    def test_switch_type_hostname_slug_normalizes_on_save(self) -> None:
        switch_type = _make_switch_type(hostname_slug=" SG300-10MP ")
        self.assertEqual(switch_type.hostname_slug, "sg300-10mp")

    def test_switch_type_hostname_slug_normalizes_by_full_clean(self) -> None:
        switch_type = NetworkSwitchType(
            manufacturer="Cisco", model="SG300", name="Default", port_count=0, hostname_slug=" SG300 "
        )
        switch_type.full_clean()
        self.assertEqual(switch_type.hostname_slug, "sg300")

    def test_device_type_hostname_slug_normalizes_on_save(self) -> None:
        device_type = _make_device_type(hostname_slug=" IK42 ")
        self.assertEqual(device_type.hostname_slug, "ik42")

    def test_hostname_slug_not_unique_across_two_profiles_of_one_model(self) -> None:
        _make_device_type(name="Default", hostname_slug="ik42")
        second = _make_device_type(name="with Dante Card", hostname_slug="ik42")  # must not raise
        self.assertEqual(second.hostname_slug, "ik42")

    def test_switch_type_hostname_slug_editable_after_instances_exist(self) -> None:
        switch_type = _make_switch_type(hostname_slug="sg300")
        NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw1")
        switch_type.hostname_slug = "sg300-10mp"
        switch_type.full_clean()
        switch_type.save()
        switch_type.refresh_from_db()
        self.assertEqual(switch_type.hostname_slug, "sg300-10mp")

    def test_switch_type_hostname_slug_not_locked_by_get_readonly_fields(self) -> None:
        switch_type = _make_switch_type(hostname_slug="sg300")
        NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw1")
        admin = NetworkSwitchTypeAdmin(NetworkSwitchType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), switch_type)
        self.assertIn("manufacturer", readonly)  # sanity — the lock is real
        self.assertNotIn("hostname_slug", readonly)

    def test_device_type_hostname_slug_editable_after_instances_exist(self) -> None:
        device_type = _make_device_type(hostname_slug="ik42")
        NetworkDevice.objects.create(device_type=device_type, hostname="dev1")
        device_type.hostname_slug = "ik-42"
        device_type.full_clean()
        device_type.save()
        device_type.refresh_from_db()
        self.assertEqual(device_type.hostname_slug, "ik-42")

    def test_device_type_hostname_slug_not_locked_by_get_readonly_fields(self) -> None:
        device_type = _make_device_type(hostname_slug="ik42")
        NetworkDevice.objects.create(device_type=device_type, hostname="dev1")
        admin = NetworkDeviceTypeAdmin(NetworkDeviceType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), device_type)
        self.assertIn("manufacturer", readonly)  # sanity — the lock is real
        self.assertNotIn("hostname_slug", readonly)

    def test_invalid_hostname_slug_rejected(self) -> None:
        device_type = NetworkDeviceType(
            manufacturer="M", model="M", name="Default", port_count=0, hostname_slug="-bad"
        )
        with self.assertRaises(ValidationError):
            device_type.full_clean()


class HostnamePurposeSequenceTests(TestCase):
    """ADR 0023 decisions 1/3 — ``hostname_purpose`` normalises and
    validates; ``hostname_sequence`` accepts ``None`` and rejects a
    negative via ``full_clean()``, stated at that layer deliberately since
    settled decision 6 exists because ``save()`` bypasses ``clean()``.
    """

    def setUp(self) -> None:
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_switch_hostname_purpose_normalizes_on_save(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, hostname="sw1", hostname_purpose=" SUB "
        )
        self.assertEqual(switch.hostname_purpose, "sub")

    def test_switch_hostname_purpose_normalizes_by_full_clean(self) -> None:
        switch = NetworkSwitch(switch_type=self.switch_type, hostname="sw1", hostname_purpose=" SUB ")
        switch.full_clean()
        self.assertEqual(switch.hostname_purpose, "sub")

    def test_device_hostname_purpose_normalizes_on_save(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type, hostname="dev1", hostname_purpose=" MIDHI-01-04 "
        )
        self.assertEqual(device.hostname_purpose, "midhi-01-04")

    def test_invalid_hostname_purpose_rejected(self) -> None:
        device = NetworkDevice(device_type=self.device_type, hostname="dev1", hostname_purpose="-bad")
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_device_hostname_sequence_accepts_none(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, hostname="dev1")
        self.assertIsNone(device.hostname_sequence)

    def test_device_hostname_sequence_negative_rejected_by_full_clean(self) -> None:
        device = NetworkDevice(device_type=self.device_type, hostname="dev1", hostname_sequence=-1)
        with self.assertRaises(ValidationError):
            device.full_clean()

    def test_switch_hostname_sequence_negative_rejected_by_full_clean(self) -> None:
        switch = NetworkSwitch(switch_type=self.switch_type, hostname="sw1", hostname_sequence=-1)
        with self.assertRaises(ValidationError):
            switch.full_clean()


class HostnameIngredientFormFieldPresenceTests(TestCase):
    """The guard rev 1 lacked: ``owner``, ``hostname_purpose`` and
    ``hostname_sequence`` each appear in the rendered form's
    ``base_fields`` on both the add and change views, for both device and
    switch — the ``base_fields`` idiom already used elsewhere in this file.
    Without this, the owner-default tests below would pass vacuously on a
    field the form never had (``NetworkDeviceAddForm.Meta.fields`` is an
    explicit list; ``construct_instance()`` silently drops anything left
    out of it).
    """

    def setUp(self) -> None:
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def test_device_add_form_has_the_three_fields(self) -> None:
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        request = RequestFactory().get("/admin/inventory/networkdevice/add/")
        form_class = admin.get_form(request, None)
        for field in ("owner", "hostname_purpose", "hostname_sequence"):
            self.assertIn(field, form_class.base_fields)

    def test_device_change_form_has_the_three_fields(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type, hostname="dev1")
        admin = NetworkDeviceAdmin(NetworkDevice, AdminSite())
        request = RequestFactory().get(f"/admin/inventory/networkdevice/{device.pk}/change/")
        form_class = admin.get_form(request, device)
        for field in ("owner", "hostname_purpose", "hostname_sequence"):
            self.assertIn(field, form_class.base_fields)

    def test_switch_add_form_has_the_three_fields(self) -> None:
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        request = RequestFactory().get("/admin/inventory/networkswitch/add/")
        form_class = admin.get_form(request, None)
        for field in ("owner", "hostname_purpose", "hostname_sequence"):
            self.assertIn(field, form_class.base_fields)

    def test_switch_change_form_has_the_three_fields(self) -> None:
        switch = NetworkSwitch.objects.create(switch_type=self.switch_type, hostname="sw1")
        admin = NetworkSwitchAdmin(NetworkSwitch, AdminSite())
        request = RequestFactory().get(f"/admin/inventory/networkswitch/{switch.pk}/change/")
        form_class = admin.get_form(request, switch)
        for field in ("owner", "hostname_purpose", "hostname_sequence"):
            self.assertIn(field, form_class.base_fields)


class RackDerivedOwnerDefaultTests(TestCase):
    """Plan settled decision 2: ``NetworkDeviceAddForm``/
    ``NetworkSwitchAddForm`` fill a blank ``owner`` from ``rack.owner`` at
    creation — a default, not inheritance (ADR 0019's suggest-don't-lock).
    An explicit choice is never overridden; the change form never derives;
    ``objects.create()`` never derives; a rack with no owner leaves it
    blank.
    """

    def setUp(self) -> None:
        self.owner = Owner.objects.create(slug="mps", name="MPS")
        self.other_owner = Owner.objects.create(slug="bej", name="BEJ")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10, owner=self.owner)
        self.ownerless_rack = Rack.objects.create(name="Rack 2", slot_count=10)
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type()

    def _device_data(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "device_type": str(self.device_type.pk),
            "hostname": "dev",
            "rack": str(self.rack.pk),
            "rack_slot": "1",
            "port_addressing": "dhcp",
        }
        data.update(overrides)
        return data

    def _switch_data(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "switch_type": str(self.switch_type.pk),
            "hostname": "sw",
            "rack": str(self.rack.pk),
            "rack_slot": "1",
            "address_materialization": "manual",
        }
        data.update(overrides)
        return data

    def test_device_add_form_fills_blank_owner_from_rack(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.owner, self.owner)

    def test_device_add_form_does_not_override_explicit_owner(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data(owner=str(self.other_owner.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.owner, self.other_owner)

    def test_device_add_form_leaves_owner_blank_for_ownerless_rack(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data(rack=str(self.ownerless_rack.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.instance.owner)

    def test_device_change_form_does_not_derive_owner(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="dev1"
        )
        self.assertIsNone(device.owner)
        form = NetworkDeviceChangeForm(
            instance=device,
            data={
                "device_type": str(device.device_type_id),
                "hostname": device.hostname,
                "serial_number": device.serial_number,
                "rack": str(self.rack.pk),
                "rack_slot": "1",
            },
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.instance.owner)

    def test_device_objects_create_does_not_derive_owner(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="dev1"
        )
        self.assertIsNone(device.owner)

    def test_switch_add_form_fills_blank_owner_from_rack(self) -> None:
        form = NetworkSwitchAddForm(data=self._switch_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.owner, self.owner)

    def test_switch_add_form_does_not_override_explicit_owner(self) -> None:
        form = NetworkSwitchAddForm(data=self._switch_data(owner=str(self.other_owner.pk)))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.owner, self.other_owner)

    def test_switch_objects_create_does_not_derive_owner(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="sw1"
        )
        self.assertIsNone(switch.owner)


class HostnameIngredientAuditTests(TestCase):
    """ADR 0023 Consequences / review note 6: ``owner``, ``hostname_purpose``
    and ``hostname_sequence`` join ``NetworkSwitch``/``NetworkDevice``'s
    existing scoped ``include_fields`` whitelists — without this they'd be
    silently untracked. ``Owner`` itself is bare-registered. Shape follows
    ``DepartmentAuditTests``.
    """

    def test_device_owner_change_produces_update_log_entry_naming_owner(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        device_type = _make_device_type()
        device = NetworkDevice.objects.create(device_type=device_type, hostname="dev1")
        LogEntry.objects.filter(object_pk=str(device.pk)).delete()
        device.owner = owner
        device.save()
        entries = LogEntry.objects.filter(object_pk=str(device.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entries.count(), 1)
        self.assertIn("owner", entries.first().changes_dict)

    def test_switch_owner_change_produces_update_log_entry_naming_owner(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        switch_type = _make_switch_type()
        switch = NetworkSwitch.objects.create(switch_type=switch_type, hostname="sw1")
        LogEntry.objects.filter(object_pk=str(switch.pk)).delete()
        switch.owner = owner
        switch.save()
        entries = LogEntry.objects.filter(object_pk=str(switch.pk), action=LogEntry.Action.UPDATE)
        self.assertEqual(entries.count(), 1)
        self.assertIn("owner", entries.first().changes_dict)

    def test_owner_itself_is_tracked(self) -> None:
        owner = Owner.objects.create(slug="mps", name="MPS")
        LogEntry.objects.filter(object_pk=str(owner.pk)).delete()
        owner.name = "MPS Audio"
        owner.save()
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(owner.pk), action=LogEntry.Action.UPDATE).exists()
        )


class HostnameComputesNothingYetTests(TestCase):
    """The phase-18 guard: creating a device/switch with every hostname
    component set leaves ``hostname`` exactly as submitted — blank
    included. If this ever fails, hostname assembly has leaked backward
    into phase 17, which builds fields and computes nothing.
    """

    def setUp(self) -> None:
        self.owner = Owner.objects.create(slug="mps", name="MPS")
        self.rack = Rack.objects.create(
            name="Rack 1", slot_count=10, owner=self.owner, location_slug="wpcsrl"
        )
        self.switch_type = _make_switch_type(hostname_slug="sg300")
        self.device_type = _make_device_type(hostname_slug="ik42")

    def test_device_hostname_left_exactly_as_submitted(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            hostname="Some Prose Hostname",
            owner=self.owner,
            hostname_purpose="sub",
            hostname_sequence=1,
        )
        self.assertEqual(device.hostname, "Some Prose Hostname")

    def test_device_hostname_left_blank_when_submitted_blank(self) -> None:
        device = NetworkDevice.objects.create(
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=1,
            hostname="",
            owner=self.owner,
            hostname_purpose="sub",
            hostname_sequence=1,
        )
        self.assertEqual(device.hostname, "")

    def test_switch_hostname_left_exactly_as_submitted(self) -> None:
        switch = NetworkSwitch.objects.create(
            switch_type=self.switch_type,
            rack=self.rack,
            rack_slot=2,
            hostname="Some Prose Hostname",
            owner=self.owner,
            hostname_purpose="sub",
            hostname_sequence=1,
        )
        self.assertEqual(switch.hostname, "Some Prose Hostname")


class RackTemplateModelTests(TestCase):
    """ADR 0014 decisions 1-3: name strip/uniqueness, both slot_count
    bounds (Rack's and RackTemplate's — separate fields, separate tests, so
    one can't accidentally stand in for the other), and L2-only VLAN
    rejection tested against all three write paths that can add a
    (template, VLAN) link, since none of them can live in
    ``RackTemplateVlan.clean()`` alone.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.l2_vlan = VLAN.objects.create(name="L2 Only", vlan_id=999, subnet="")

    def test_name_is_stripped_on_save(self) -> None:
        template = RackTemplate.objects.create(name="  Audio Rack  ")
        self.assertEqual(template.name, "Audio Rack")

    def test_name_is_stripped_before_uniqueness_check(self) -> None:
        RackTemplate.objects.create(name="Audio Rack")
        duplicate = RackTemplate(name="Audio Rack  ")
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_blank_name_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            RackTemplate.objects.create(name="")

    def test_duplicate_template_vlan_pair_rejected(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        with self.assertRaises(IntegrityError):
            RackTemplateVlan.objects.create(template=template, vlan=self.vlan)

    # Rack.slot_count's own >= 1 bound (ADR 0014 decision 9's settled
    # question) — distinct from RackTemplate.slot_count's below.
    def test_rack_slot_count_zero_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            Rack.objects.create(name="Rack 1", slot_count=0)

    # RackTemplate.slot_count's own >= 1 bound — a different field on a
    # different model; must not be satisfied by the test above alone.
    def test_rack_template_slot_count_zero_rejected_by_validator(self) -> None:
        template = RackTemplate(name="Bad", slot_count=0)
        with self.assertRaises(ValidationError):
            template.full_clean()

    def test_rack_template_slot_count_zero_rejected_by_db_constraint(self) -> None:
        with self.assertRaises(IntegrityError):
            RackTemplate.objects.create(name="Bad DB", slot_count=0)

    # Path 1: direct through-row clean().
    def test_direct_through_row_rejects_l2_only_vlan_via_clean(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        link = RackTemplateVlan(template=template, vlan=self.l2_vlan)
        with self.assertRaises(ValidationError):
            link.full_clean()

    # Path 1, bypassing clean(): the through row's own save().
    def test_direct_through_row_save_rejects_l2_only_vlan_bypassing_clean(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        with self.assertRaises(ValidationError):
            RackTemplateVlan.objects.create(template=template, vlan=self.l2_vlan)

    # Path 2: the m2m_changed receiver — .add()/.set() never call
    # RackTemplateVlan.save() at all.
    def test_add_rejects_l2_only_vlan(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        with self.assertRaises(ValidationError):
            template.vlans.add(self.l2_vlan)

    def test_set_rejects_l2_only_vlan(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        with self.assertRaises(ValidationError):
            template.vlans.set([self.vlan, self.l2_vlan])

    def test_add_allows_ordinary_vlan(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        template.vlans.add(self.vlan)  # must not raise
        self.assertIn(self.vlan, template.vlans.all())

    # Path 3: the admin form's clean_vlans().
    def test_admin_form_rejects_l2_only_vlan(self) -> None:
        form = RackTemplateForm(data={"name": "Audio Rack", "vlans": [self.l2_vlan.pk]})
        self.assertFalse(form.is_valid())

    def test_admin_form_accepts_ordinary_vlan(self) -> None:
        form = RackTemplateForm(data={"name": "Audio Rack", "vlans": [self.vlan.pk]})
        self.assertTrue(form.is_valid(), form.errors)


class RackTemplateApplicationTests(TestCase):
    """ADR 0014's two mandated cases — a successful multi-VLAN apply
    through the construct-blank -> full_clean() -> save() path, and a
    rollback leaving no rack and no ranges when one of several VLANs can't
    be allocated — plus the surrounding behavior the implementation plan
    calls out: zero-VLAN no-op, the programmatic path (with and without
    slot_count), seed-once (editing/deleting a template after apply has no
    effect), and the pre-flight naming every failing VLAN, not just the
    first.
    """

    def setUp(self) -> None:
        self.vlan_a = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.vlan_b = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        # A /27 (32 addresses) can never fit a slot_count large enough to
        # also need more than vlan_a/vlan_b's /21s — used below to force an
        # allocation failure on exactly one VLAN of several.
        self.tiny_vlan = VLAN.objects.create(name="Tiny", vlan_id=202, subnet="10.202.1.0/27")

    def test_apply_creates_ranges_via_suggestion_path(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_a)
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_b)
        # slot_count=30 -> required_block_size=32 -> a /27 block, matching
        # RackVlanRangeSuggestionTests' fixture so the expected suggestion
        # here is verified against the same known-good arithmetic.
        rack = Rack(name="Rack 1", slot_count=30)
        rack.template = template
        rack.save()
        self.assertEqual(
            RackVlanRange.objects.get(rack=rack, vlan=self.vlan_a).address_range, "10.200.0.0/27"
        )
        self.assertEqual(
            RackVlanRange.objects.get(rack=rack, vlan=self.vlan_b).address_range, "10.201.0.0/27"
        )

    def test_apply_rolls_back_rack_and_ranges_when_one_vlan_unallocatable(self) -> None:
        template = RackTemplate.objects.create(name="Mixed")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_a)
        RackTemplateVlan.objects.create(template=template, vlan=self.tiny_vlan)
        # slot_count=1000 needs a /22-sized block: fits inside vlan_a's
        # /21, but is bigger than tiny_vlan's whole /27 subnet.
        rack = Rack(name="Rack 2", slot_count=1000)
        rack.template = template
        with self.assertRaises(ValidationError):
            rack.save()
        self.assertFalse(Rack.objects.filter(name="Rack 2").exists())
        self.assertFalse(RackVlanRange.objects.filter(vlan=self.vlan_a).exists())
        self.assertFalse(RackVlanRange.objects.filter(vlan=self.tiny_vlan).exists())

    def test_preflight_error_names_all_unallocatable_vlans(self) -> None:
        tiny_vlan_2 = VLAN.objects.create(name="Tiny2", vlan_id=203, subnet="10.203.1.0/27")
        template = RackTemplate.objects.create(name="Multi-fail")
        RackTemplateVlan.objects.create(template=template, vlan=self.tiny_vlan)
        RackTemplateVlan.objects.create(template=template, vlan=tiny_vlan_2)
        rack = Rack(name="Rack 3", slot_count=1000)
        rack.template = template
        with self.assertRaises(ValidationError) as ctx:
            rack.save()
        message = str(ctx.exception)
        self.assertIn(str(self.tiny_vlan), message)
        self.assertIn(str(tiny_vlan_2), message)

    def test_apply_is_noop_for_zero_vlan_template(self) -> None:
        empty_template = RackTemplate.objects.create(name="Empty")
        rack = Rack(name="Rack 4", slot_count=4)
        rack.template = empty_template
        rack.save()  # must not raise
        self.assertFalse(RackVlanRange.objects.filter(rack=rack).exists())

    def test_objects_create_with_template_kwarg_applies_template(self) -> None:
        template = RackTemplate.objects.create(name="Programmatic")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_a)
        # template is a property, not a real field — django-stubs doesn't
        # recognize it as a valid objects.create() kwarg.
        rack = Rack.objects.create(name="Rack 5", slot_count=4, template=template)  # type: ignore[misc]
        self.assertTrue(RackVlanRange.objects.filter(rack=rack, vlan=self.vlan_a).exists())

    def test_objects_create_with_template_but_no_slot_count_still_raises(self) -> None:
        # The template HAS a slot_count — proves it's not consulted outside
        # the admin form's fallback (grilling decision 2's domain-scope
        # boundary: only the VLAN list is a domain-level concern).
        template = RackTemplate.objects.create(name="No Slot Count", slot_count=8)
        with self.assertRaises(IntegrityError):
            Rack.objects.create(name="Rack 6", template=template)  # type: ignore[misc]

    def test_editing_template_after_apply_does_not_affect_existing_rack(self) -> None:
        template = RackTemplate.objects.create(name="Solo")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_a)
        rack = Rack(name="Rack 7", slot_count=4)
        rack.template = template
        rack.save()
        original_range = RackVlanRange.objects.get(rack=rack, vlan=self.vlan_a).address_range

        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_b)  # edit after the fact

        rack.refresh_from_db()
        self.assertEqual(RackVlanRange.objects.filter(rack=rack).count(), 1)
        self.assertEqual(RackVlanRange.objects.get(rack=rack, vlan=self.vlan_a).address_range, original_range)
        self.assertFalse(RackVlanRange.objects.filter(rack=rack, vlan=self.vlan_b).exists())

    def test_deleting_template_after_apply_does_not_affect_existing_rack(self) -> None:
        template = RackTemplate.objects.create(name="Ephemeral")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan_a)
        rack = Rack(name="Rack 8", slot_count=4)
        rack.template = template
        rack.save()

        template.delete()

        self.assertTrue(Rack.objects.filter(pk=rack.pk).exists())
        self.assertTrue(RackVlanRange.objects.filter(rack=rack, vlan=self.vlan_a).exists())


class RackTemplateAdminTests(TestCase):
    """The admin add-form/change-form split (template only makes sense at
    creation, ADR 0014 decision 5), the slot_count blank-submit fallback
    (decision 9, as amended), and decision 11's template/manual-inline
    collision check — including the grilling-decision-6 guard: the
    duplicate-VLAN test asserts a *form error* specifically, not just "an
    exception of some kind", so a future Django admin internals change that
    breaks the ordering RackVlanRangeInlineFormSet depends on fails loudly
    here instead of silently degrading to a raw IntegrityError.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_template_field_present_on_add_form(self) -> None:
        form_class = RackAdmin(Rack, AdminSite()).get_form(RequestFactory().get("/"), obj=None)
        self.assertIn("template", form_class.base_fields)

    def test_template_field_absent_on_change_form(self) -> None:
        rack = Rack.objects.create(name="Rack 1", slot_count=4)
        form_class = RackAdmin(Rack, AdminSite()).get_form(RequestFactory().get("/"), obj=rack)
        self.assertNotIn("template", form_class.base_fields)

    def test_blank_slot_count_adopts_template_value(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack", slot_count=8)
        form = RackAddForm(data={"name": "Rack 2", "slot_count": "", "template": str(template.pk)})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.slot_count, 8)

    def test_explicit_slot_count_is_not_overwritten_by_template(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack", slot_count=8)
        form = RackAddForm(data={"name": "Rack 3", "slot_count": "4", "template": str(template.pk)})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.slot_count, 4)

    def test_switching_template_on_resubmit_adopts_new_templates_value(self) -> None:
        RackTemplate.objects.create(name="Audio Rack", slot_count=8)
        template_b = RackTemplate.objects.create(name="Video Rack", slot_count=12)
        form = RackAddForm(data={"name": "Rack 4", "slot_count": "", "template": str(template_b.pk)})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.slot_count, 12)

    def test_no_template_and_blank_slot_count_is_required_error(self) -> None:
        form = RackAddForm(data={"name": "Rack 5", "slot_count": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("slot_count", form.errors)

    def test_duplicate_vlan_across_template_and_manual_inline_is_form_error(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        form = RackAddForm(data={"name": "Rack 6", "slot_count": "4", "template": str(template.pk)})
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            Rack,
            RackVlanRange,
            formset=RackVlanRangeInlineFormSet,
            fields=["vlan", "address_range"],
            extra=1,
            can_delete=True,
        )
        data = {
            "vlan_ranges-TOTAL_FORMS": "1",
            "vlan_ranges-INITIAL_FORMS": "0",
            "vlan_ranges-MIN_NUM_FORMS": "0",
            "vlan_ranges-MAX_NUM_FORMS": "1000",
            "vlan_ranges-0-vlan": str(self.vlan.pk),
            "vlan_ranges-0-address_range": "",
        }
        # Mirrors Django's real admin ordering (see
        # RackVlanRangeInlineFormSet's docstring): the formset is
        # constructed against form.instance *before* form.is_valid() runs —
        # capture that reference here, then validate the form, then the
        # formset, in that exact order.
        formset = FormSet(data, instance=form.instance, prefix="vlan_ranges")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(formset.is_valid())
        self.assertIn(str(self.vlan), str(formset.non_form_errors()))

    def test_non_conflicting_manual_inline_alongside_template_is_valid(self) -> None:
        other_vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        form = RackAddForm(data={"name": "Rack 7", "slot_count": "4", "template": str(template.pk)})
        FormSet = inlineformset_factory(  # type: ignore[var-annotated]
            Rack,
            RackVlanRange,
            formset=RackVlanRangeInlineFormSet,
            fields=["vlan", "address_range"],
            extra=1,
            can_delete=True,
        )
        data = {
            "vlan_ranges-TOTAL_FORMS": "1",
            "vlan_ranges-INITIAL_FORMS": "0",
            "vlan_ranges-MIN_NUM_FORMS": "0",
            "vlan_ranges-MAX_NUM_FORMS": "1000",
            "vlan_ranges-0-vlan": str(other_vlan.pk),
            "vlan_ranges-0-address_range": "",
        }
        formset = FormSet(data, instance=form.instance, prefix="vlan_ranges")
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(formset.is_valid(), formset.errors)


class RackTemplateDeletionTests(TestCase):
    """ADR 0014 decisions 2/4: a Rack Template is freely deletable and its
    membership cascades (the inverse direction — a listed VLAN is
    PROTECTed against deletion, and against having its subnet blanked).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_template_freely_deletable(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        template.delete()  # must not raise
        self.assertFalse(RackTemplate.objects.filter(pk=template.pk).exists())

    def test_deleting_template_cascades_membership_rows(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        link = RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        template.delete()
        self.assertFalse(RackTemplateVlan.objects.filter(pk=link.pk).exists())

    def test_vlan_removal_blocked_while_listed_in_template(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        with self.assertRaises(ProtectedError):
            self.vlan.delete()

    def test_blanking_listed_vlans_subnet_is_blocked(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        self.vlan.subnet = ""
        with self.assertRaises(ValidationError):
            self.vlan.full_clean()


class RackTemplateAuditTests(TestCase):
    """ADR 0014 decision 12: RackTemplate/RackTemplateVlan mutations are in
    ADR 0004's audit scope, not just creation — the profile's own
    m2m_fields registration covers .add()/.set() (which fire m2m_changed),
    and RackTemplateVlan's own registration covers direct through-row
    writes (which never fire that signal), mirroring
    SwitchPortVlanProfile's exact registration split
    (ReviewCouncilRegressionTests above, for the same reason).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")

    def test_template_creation_is_logged(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(template.pk), action=LogEntry.Action.CREATE).exists()
        )

    def test_vlans_add_is_logged(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        template.vlans.add(self.vlan)
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(template.pk), action=LogEntry.Action.UPDATE).exists()
        )

    def test_direct_through_row_creation_is_logged(self) -> None:
        template = RackTemplate.objects.create(name="Audio Rack")
        link = RackTemplateVlan.objects.create(template=template, vlan=self.vlan)
        self.assertTrue(
            LogEntry.objects.filter(object_pk=str(link.pk), action=LogEntry.Action.CREATE).exists()
        )


class RackAddressingProductionReplayTests(TestCase):
    """The evidentiary basis for ADR 0015: replaying production's racks
    through the real suggester (not the pure function in isolation)
    reproduces 19 of its 21 rack bases automatically, versus 1 of 21
    without the /27 floor. This is the test that must fail loudly if the
    floor's arithmetic ever drifts.

    Built end-to-end through the model layer — Rack, then
    RackVlanRange(...).full_clean()/.save() — rather than by calling
    suggest_rack_vlan_range() directly, because the pure-function version
    would pass even if _validate_range() disagreed with the suggester,
    which is precisely the class of drift this test guards against.

    Slot counts and bases are transcribed as literals from
    PROD-DATA-ANALYSIS.md §1 (slot counts are each rack's maximum occupied
    slot, from prod/MPS Audio Network Standards - IP Addressing mk2.csv).
    prod/ is gitignored, so those source files don't exist in CI — the
    numbers below are the only record that survives here.
    """

    def setUp(self) -> None:
        # 10.200.0.0/21, DHCP occupying the bottom /24 (ADR 0011) — this is
        # what pushes the first rack's block to 10.200.1.0 rather than
        # 10.200.0.0.
        self.vlan = VLAN.objects.create(
            name="Control",
            vlan_id=200,
            subnet="10.200.0.0/21",
            dhcp_range_start="10.200.0.1",
            dhcp_range_end="10.200.0.254",
        )

    def _create_rack(self, name: str, slot_count: int) -> tuple[Rack, RackVlanRange]:
        rack = Rack.objects.create(name=name, slot_count=slot_count)
        range_ = RackVlanRange(rack=rack, vlan=self.vlan)
        range_.full_clean()
        range_.save()
        return rack, range_

    def test_replays_19_of_21_production_bases_with_floor(self) -> None:
        # slot_count is each rack's honest, as-built occupancy; expected is
        # the production base it must reproduce. Creation order matters —
        # suggest_rack_vlan_range() is first-fit, so it determines every
        # base that follows.
        racks = [
            ("CONTROL", 1, "10.200.1.0/27"),
            ("WPC1SRU", 5, "10.200.1.32/27"),
            ("WPC2SRL", 5, "10.200.1.64/27"),
            ("WPC3SLU", 5, "10.200.1.96/27"),
            ("WPC4SLL", 5, "10.200.1.128/27"),
            ("WPM1SR", 4, "10.200.1.160/27"),
            ("WPM2SL", 4, "10.200.1.192/27"),
            ("WPM3", 4, "10.200.1.224/27"),
            ("XE300-1", 4, "10.200.2.0/27"),
            ("XE300-2", 4, "10.200.2.32/27"),
            ("W8LM1SR", 3, "10.200.2.64/27"),
            ("W8LM2SL", 3, "10.200.2.96/27"),
            ("W8LM3", 3, "10.200.2.128/27"),
            ("FOH Drive #1", 2, "10.200.2.160/27"),
            ("FOH Drive #2", 2, "10.200.2.192/27"),
            ("CDD", 1, "10.200.2.224/27"),
            ("AVIO", 19, "10.200.3.0/27"),
            ("SPARE", 3, "10.200.3.32/27"),
            ("FLOATSWITCH", 3, "10.200.3.64/27"),
        ]
        # CONTROL, CDD and SHURE hold no equipment, so their honest slot
        # count isn't derivable from the addressing CSV; slot_count=1 is
        # used for CONTROL and CDD above because Rack.slot_count has
        # MinValueValidator(1). This is safe rather than a guess that
        # matters: under the floor, every slot count from 1 to 30 yields a
        # /27 (see test_required_block_size_and_prefix_floored_below_slot_
        # count_30 above), and production occupancy here runs 2-19 — no
        # value in the plausible range could change a single expected base.
        for name, slot_count, expected_base in racks:
            _rack, range_ = self._create_rack(name, slot_count)
            self.assertEqual(range_.address_range, expected_base, f"{name} (slot_count={slot_count})")

        # Without the floor, this replay reproduces 1 of 21 bases — and the
        # one that survives is CONTROL, purely because it is created first:
        # the first free block above the DHCP /24 starts at 10.200.1.0 no
        # matter what size it is, so its *base* matches production even
        # though the block itself would be a /30. Every rack after it would
        # size its own, smaller block instead, and every later base would
        # drift off the production offsets as a result.
        self.assertEqual(prefix_length_for_capacity(2), 27)

        # SHURE (offset 1280 = 10.200.5.0) and CONSOLES (offset 1536 =
        # 10.200.6.0) sit behind deliberate reserved gaps — a first-fit
        # suggester never leaves a gap, so those two must be entered by hand
        # (ADR 0001 working as intended, not a shortfall of the floor). The
        # 20th rack must keep packing sequentially rather than jumping to
        # either gap.
        _rack, twentieth = self._create_rack("Twentieth Rack", 3)
        self.assertEqual(twentieth.address_range, "10.200.3.96/27")


class RackSlotSuggestionTests(TestCase):
    """PLAN-adr-0019.md's verification list (ADR 0019) — items 2-13. Item 1
    (``lowest_free_run`` itself) joins ``SuggestionFunctionTests`` above,
    and item 14 needs the admin add URL over the test client — see
    ``RackSlotSuggestionAdminViewTests`` below. Uses the ``Form(data=
    {...})`` style ``RackAddForm``'s own tests use (``RackTemplateAdminTests``
    above), not admin HTTP calls — ``clean()`` runs identically either way
    and this is more direct.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.other_vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=6)
        self.tiny_rack = Rack.objects.create(name="Tiny Rack", slot_count=1)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")
        self.switch_type = _make_switch_type()
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan, name="Plain")  # span 1

    def _make_spanning_type(self, vlan: VLAN | None = None, **kwargs) -> NetworkDeviceType:
        """A span-2 type: an offset-0 and an offset-1 port on the same VLAN
        (ADR 0017's SD12-shaped example — see ``SlotOffsetAddressingTests``).
        """
        vlan = vlan or self.vlan
        kwargs.setdefault("manufacturer", "DiGiCo")
        kwargs.setdefault("model", "SD12")
        device_type = NetworkDeviceType.objects.create(port_count=2, **kwargs)
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Control",
            port_type=PortType.GBE_RJ45,
            vlan=vlan,
            slot_offset=0,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=vlan,
            slot_offset=1,
        )
        return device_type

    def _switch_data(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "switch_type": str(self.switch_type.pk),
            "hostname": "sw",
            "rack": str(self.rack.pk),
            "rack_slot": "",
            "address_materialization": "manual",
        }
        data.update(overrides)
        return data

    def _device_data(self, device_type: NetworkDeviceType | None = None, **overrides: Any) -> dict[str, Any]:
        device_type = device_type or self.device_type
        data: dict[str, Any] = {
            "device_type": str(device_type.pk),
            "hostname": "dev",
            "rack": str(self.rack.pk),
            "rack_slot": "",
            "port_addressing": "dhcp",
        }
        data.update(overrides)
        return data

    # -- 2/3: a plain device/switch takes the lowest free ordinal, and the
    # suggested value actually reaches the instance -----------------------------------

    def test_plain_device_gets_lowest_free_ordinal_on_the_instance(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 1)

    def test_plain_switch_gets_lowest_free_ordinal_on_the_instance(self) -> None:
        form = NetworkSwitchAddForm(data=self._switch_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 1)

    # -- 4: a spanning device (ADR 0017) skips a run that would overlap ----------------

    def test_spanning_device_skips_a_run_that_would_overlap(self) -> None:
        NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="d1")
        NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=3, hostname="s1")
        spanning_type = self._make_spanning_type(name="Spanner")
        form = NetworkDeviceAddForm(data=self._device_data(device_type=spanning_type))
        self.assertTrue(form.is_valid(), form.errors)
        # The gap at 2 is only 1 slot wide (3 is taken) — too small for a
        # span of 2, so the first fit is 4, not 2.
        self.assertEqual(form.instance.rack_slot, 4)

    # -- 5: an already-stored span-2 device contributes its whole range ----------------

    def test_stored_spanning_device_contributes_its_whole_range(self) -> None:
        spanning_type = self._make_spanning_type(name="Spanner2")
        NetworkDevice.objects.create(device_type=spanning_type, rack=self.rack, rack_slot=1, hostname="span1")
        form = NetworkSwitchAddForm(data=self._switch_data())
        self.assertTrue(form.is_valid(), form.errors)
        # The span-2 device at 1 occupies 1-2 — an implementation treating
        # it as span 1 would offer 2, not 3.
        self.assertEqual(form.instance.rack_slot, 3)

    # -- 6: an operator-typed ordinal still wins ---------------------------------------

    def test_typed_device_rack_slot_is_not_overwritten(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data(rack_slot="5"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 5)

    def test_typed_switch_rack_slot_is_not_overwritten(self) -> None:
        form = NetworkSwitchAddForm(data=self._switch_data(rack_slot="5"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 5)

    # -- 7: cross-table — a switch blocks a device's suggestion and vice versa --------

    def test_switch_blocks_a_devices_suggestion(self) -> None:
        NetworkSwitch.objects.create(switch_type=self.switch_type, rack=self.rack, rack_slot=1, hostname="s1")
        form = NetworkDeviceAddForm(data=self._device_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 2)

    def test_device_blocks_a_switchs_suggestion(self) -> None:
        NetworkDevice.objects.create(device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="d1")
        form = NetworkSwitchAddForm(data=self._switch_data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.rack_slot, 2)

    # -- 11: rack left blank suggests nothing; still valid (spare pool) ---------------

    def test_blank_rack_suggests_nothing_for_a_device(self) -> None:
        form = NetworkDeviceAddForm(data=self._device_data(rack="", rack_slot=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.instance.rack)
        self.assertIsNone(form.instance.rack_slot)

    def test_blank_rack_suggests_nothing_for_a_switch(self) -> None:
        form = NetworkSwitchAddForm(data=self._switch_data(rack="", rack_slot=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIsNone(form.instance.rack)
        self.assertIsNone(form.instance.rack_slot)

    # -- 12: the ordinal-only boundary (decision 5) — a suggested-free ordinal can
    # still be rejected by _validate_static_address ------------------------------------

    def test_ordinal_only_boundary_a_free_ordinal_can_still_be_address_rejected(self) -> None:
        device_a = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="deviceA"
        )
        port_a = device_a.ports.get()
        # Edit ordinal 1's own stored address to what ordinal 2's
        # arithmetic would produce (ADR 0003 makes addresses editable).
        port_a.address = "10.200.1.2"
        port_a.full_clean()
        port_a.save()

        form = NetworkDeviceAddForm(data=self._device_data(hostname="deviceB", port_addressing="static"))
        self.assertFalse(form.is_valid())
        # The suggestion itself succeeded — ordinal 2 is unoccupied — and
        # landed on the instance; the rejection comes from address
        # uniqueness, a different and later check, not from rack_slot.
        self.assertNotIn("rack_slot", form.errors)
        self.assertEqual(form.instance.rack_slot, 2)
        self.assertIn("already assigned to", str(form.errors))

    # -- 13: the change form is unaffected — moving equipment auto-suggests nothing --

    def test_change_form_does_not_auto_suggest_on_blank_rack_slot(self) -> None:
        rack2 = Rack.objects.create(name="Rack 2", slot_count=6)
        device = NetworkDevice.objects.create(
            device_type=self.device_type, rack=self.rack, rack_slot=1, hostname="mover"
        )
        form = NetworkDeviceChangeForm(
            instance=device,
            data={
                "device_type": str(device.device_type_id),
                "hostname": device.hostname,
                "serial_number": device.serial_number,
                "rack": str(rack2.pk),
                "rack_slot": "",
            },
        )
        self.assertFalse(form.is_valid())
        self.assertIn("rack and rack_slot must both be set", str(form.errors))


class RackSlotSuggestionAdminViewTests(TestCase):
    """Verification 14 (ADR 0019/ADR 0020 decision 3): a deep link's query
    parameters prefill both ``rack`` and ``rack_slot`` on the real admin add
    view. This is plain ``ModelAdmin.get_changeform_initial_data()``
    behaviour — nothing this plan adds — pinned here before phase 15 builds
    a deep link on top of it. The only test in this ADR that needs the
    admin add URL over the test client rather than ``Form(data={...})``:
    there is no submission here to reach a bare form's ``clean()`` with.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("adr19admin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="adr19admin", password="testpass123")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=6)

    def test_get_query_params_prefill_rack_and_rack_slot(self) -> None:
        response = self.client.get(f"/admin/inventory/networkdevice/add/?rack={self.rack.pk}&rack_slot=6")
        self.assertEqual(response.status_code, 200)
        initial = response.context["adminform"].form.initial
        self.assertEqual(initial.get("rack"), str(self.rack.pk))
        self.assertEqual(initial.get("rack_slot"), "6")


class OperatorSetPortTests(TestCase):
    """ADR 0022 — ``NetworkDeviceTypePort.address_source`` and the
    ``NetworkDevice.operator_addresses`` transient property (issue #42): a
    second, independently-addressed static port on a VLAN a device already
    uses — a Yamaha console's Device Control interface, e.g., which shares
    its console's Dante Primary VLAN but has no derivable relationship to
    that port's address.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")

    def _make_console_type(self, **kwargs) -> NetworkDeviceType:
        """DM7C-shaped type (ADR 0022's motivating example): a SLOT-
        addressed Dante Primary and an OPERATOR-addressed Device Control,
        both on ``self.vlan`` — the exact shape issue #42 exists for.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", port_count=2, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante Primary", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )
        return device_type

    def test_operator_port_materializes_from_operator_addresses(self) -> None:
        device_type = self._make_console_type(name="DM7C Materialize")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        ports = {p.description: p for p in device.ports.all()}
        self.assertFalse(ports["Dante Primary"].is_dhcp)
        self.assertEqual(ports["Dante Primary"].address, "10.201.6.5")
        self.assertFalse(ports["Device Control"].is_dhcp)
        self.assertEqual(ports["Device Control"].address, "10.201.6.4")

    def test_operator_port_missing_address_refused_by_name(self) -> None:
        """Exercised via ``objects.create()`` directly — the path that
        never calls ``clean()`` — so this proves
        ``_check_static_materialization_possible()``'s own pre-flight
        catches the missing key, not only ``_materialize_ports()``'s.
        """
        device_type = self._make_console_type(name="DM7C Missing")
        with self.assertRaises(ValidationError) as ctx:
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=5)
        self.assertIn("Device Control", str(ctx.exception))
        self.assertEqual(NetworkDevicePort.objects.count(), 0)  # nothing written

    def test_two_ports_one_vlan_accepted_when_one_is_operator(self) -> None:
        # Materialization above would fail outright if the same-VLAN
        # refusal still caught the OPERATOR port — successfully getting
        # two ports out of one VLAN here *is* the assertion (the fix for
        # #42).
        device_type = self._make_console_type(name="DM7C Two Ports")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        self.assertEqual(device.ports.count(), 2)

    def test_full_console_shape_materializes_four_ports(self) -> None:
        """ADR 0022 PR 2 — the production DM7C/DM3 shape exactly: Control,
        Dante Primary and Dante Secondary (ordinary SLOT ports, one per
        VLAN), plus a fourth, OPERATOR-sourced Device Control port sharing
        Dante Primary's VLAN — not the trimmed two-port fixture the rest
        of this class uses to isolate the #42 mechanism itself.
        """
        control_vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        secondary_vlan = VLAN.objects.create(name="Dante Secondary", vlan_id=202, subnet="10.202.0.0/21")
        RackVlanRange.objects.create(rack=self.rack, vlan=control_vlan, address_range="10.200.6.0/27")
        RackVlanRange.objects.create(rack=self.rack, vlan=secondary_vlan, address_range="10.202.6.0/27")
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="Full Console Shape", port_count=4
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Control", port_type=PortType.GBE_RJ45, vlan=control_vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante Primary", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Dante Secondary",
            port_type=PortType.GBE_RJ45,
            vlan=secondary_vlan,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="DM7C-1",
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        ports = {p.description: p for p in device.ports.all()}
        self.assertEqual(len(ports), 4)
        self.assertEqual(ports["Control"].address, "10.200.6.5")
        self.assertEqual(ports["Dante Primary"].address, "10.201.6.5")
        self.assertEqual(ports["Dante Secondary"].address, "10.202.6.5")
        self.assertEqual(ports["Device Control"].address, "10.201.6.4")
        self.assertEqual(ports["Device Control"].hostname, "DM7C-1-device-control")

    def test_two_slot_ports_one_vlan_still_refused(self) -> None:
        """The exemption is narrow — two *SLOT* ports on one VLAN are
        still Switched Mode's shape and still refused (ADR 0017 decision
        5, untouched by this ADR).
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Shure", model="ULXD4Q", name="Two Slot Ports", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="A", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="B", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type, rack=self.rack, rack_slot=5)

    def test_operator_port_address_stays_editable_after_creation(self) -> None:
        """The exact property that distinguishes #42's shape from ADR
        0017's ``slot_offset`` ports (``NetworkDevicePort._locked_fields()``):
        an OPERATOR port's address is never derived, so nothing locks it.
        """
        device_type = self._make_console_type(name="DM7C Editable")
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        port = device.ports.get(description="Device Control")
        port.address = "10.201.6.20"
        port.save()  # must not raise
        port.refresh_from_db()
        self.assertEqual(port.address, "10.201.6.20")

    def test_operator_with_slot_offset_rejected_at_profile_check(self) -> None:
        """ADR 0022 decision 3 — an address the operator sets cannot also
        be one the hardware derives. Exercised via an unracked device,
        which skips the static pre-flight entirely but still runs
        ``_validate_device_type_port_profile()`` unconditionally (mirrors
        ``SlotOffsetAddressingTests``' orphan-offset case 12).
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Bad Offset", name="Bad Offset", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Bad",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            slot_offset=1,
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(device_type=device_type)

    def test_unracked_device_materializes_operator_port_dhcp(self) -> None:
        device_type = self._make_console_type(name="DM7C Unracked")
        device = NetworkDevice.objects.create(device_type=device_type)  # no operator_addresses given
        for port in device.ports.all():
            self.assertTrue(port.is_dhcp)
            self.assertIsNone(port.address)

    def test_operator_addresses_mapping_is_per_instance_not_shared(self) -> None:
        """Codex review of PR 1, P2 — ``_operator_addresses``'s class-level
        default is a ``dict`` (unlike ``_port_addressing``'s ``str``, which
        has no equivalent hazard since strings are immutable). A device
        created with no ``operator_addresses`` kwarg at all must not read
        back whatever another device's mapping happened to leave behind —
        proven here via two devices created in sequence, the second with
        no mapping of its own.
        """
        device_type = self._make_console_type(name="DM7C Not Shared")
        first = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=device_type,
            rack=self.rack,
            rack_slot=5,
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        second = NetworkDevice(device_type=device_type)  # unsaved — no operator_addresses given at all
        self.assertEqual(second.operator_addresses, {})
        self.assertIsNot(second.operator_addresses, first.operator_addresses)

    def test_mutating_one_devices_mapping_does_not_leak_into_another(self) -> None:
        """The sharper version of the test above: even an *in-place*
        mutation on one instance's dict-typed property — the natural way
        to write to it — must not be visible from a second instance that
        never set its own.
        """
        device_type = self._make_console_type(name="DM7C Mutation Isolation")
        first = NetworkDevice(device_type=device_type)
        second = NetworkDevice(device_type=device_type)
        first.operator_addresses["Device Control"] = "10.201.6.4"
        self.assertEqual(second.operator_addresses, {})


class IsOperatorAddressedTests(TestCase):
    """``NetworkDevicePort.is_operator_addressed`` (issue #60,
    PLAN-consumed-slot-addresses.md decision 5) — describes the port's
    *current* address, not its type port's provenance. True only for a
    non-DHCP port with a non-null address whose ``source_type_port`` is
    ``OPERATOR``-sourced.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="Operator Addressed", port_count=2
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

    def test_true_for_static_operator_port(self) -> None:
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="dm7c-1",
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        port = device.ports.get(description="Device Control")
        self.assertTrue(port.is_operator_addressed)

    def test_false_for_ordinary_static_slot_port(self) -> None:
        device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=5,
            hostname="dm7c-1",
            operator_addresses={"Device Control": "10.201.6.4"},
        )
        port = device.ports.get(description="Dante Primary")
        self.assertFalse(port.is_dhcp)
        self.assertIsNotNone(port.address)
        self.assertFalse(port.is_operator_addressed)

    def test_false_for_slot_dhcp_port(self) -> None:
        device = NetworkDevice.objects.create(device_type=self.device_type)  # unracked -> DHCP
        port = device.ports.get(description="Dante Primary")
        self.assertTrue(port.is_dhcp)
        self.assertFalse(port.is_operator_addressed)

    def test_false_for_operator_sourced_port_that_materialized_dhcp(self) -> None:
        """The case that distinguishes provenance from current state: an
        unracked device materializes DHCP for *every* port, including an
        OPERATOR-sourced one (ADR 0022's "on an unracked/DHCP device it
        materializes DHCP like any other port"). Such a port typed no
        address and consumed nothing.
        """
        device = NetworkDevice.objects.create(device_type=self.device_type)  # unracked -> DHCP
        port = device.ports.get(description="Device Control")
        self.assertTrue(port.is_dhcp)
        self.assertIsNone(port.address)
        self.assertFalse(port.is_operator_addressed)

    def test_false_for_port_with_no_source_type_port(self) -> None:
        # A directly-constructed port (models.py:3949) — no materialization
        # involved, so source_type_port is None from the start rather than
        # cleared after the fact (which _locked_fields() would refuse).
        bare_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Bare", name="Bare Port Device", port_count=0
        )
        bare_device = NetworkDevice.objects.create(device_type=bare_type)
        port = NetworkDevicePort.objects.create(
            device=bare_device, description="Hand Wired", vlan=self.vlan, address="10.201.6.4"
        )
        self.assertIsNone(port.source_type_port)
        self.assertFalse(port.is_operator_addressed)


class OperatorPortDeriveCascadeIsolationTests(TestCase):
    """Codex review of PR 1, P1 — a profile may legally hold a ``SLOT``
    offset-0 port, an ``OPERATOR`` offset-0 port and a ``SLOT`` offset>0
    port all on one VLAN (a console whose Device Control shares a VLAN
    with a Dante Primary that itself derives an engine-like sibling).
    Editing the ``OPERATOR`` port's independent address must never be
    mistaken for editing the *control* address the offset>0 sibling
    derives from; editing the genuine ``SLOT`` control address must still
    cascade exactly as ADR 0017 always has.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C+Engine", name="Cascade Isolation", port_count=3
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Dante Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Derived Sibling",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            slot_offset=1,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )
        self.device = NetworkDevice.objects.create(  # type: ignore[misc]
            device_type=self.device_type,
            rack=self.rack,
            rack_slot=5,
            operator_addresses={"Device Control": "10.201.6.4"},
        )

    def test_operator_port_edit_does_not_touch_derived_sibling(self) -> None:
        sibling_before = self.device.ports.get(description="Derived Sibling").address
        device_control = self.device.ports.get(description="Device Control")
        device_control.address = "10.201.6.20"
        device_control.save()
        sibling = self.device.ports.get(description="Derived Sibling")
        self.assertEqual(sibling.address, sibling_before)

    def test_slot_port_edit_still_cascades_to_derived_sibling(self) -> None:
        dante_primary = self.device.ports.get(description="Dante Primary")
        dante_primary.address = "10.201.6.9"
        dante_primary.save()
        sibling = self.device.ports.get(description="Derived Sibling")
        self.assertEqual(sibling.address, "10.201.6.10")


class OperatorAddressSelfCollisionTests(TestCase):
    """Codex review of PR 1, P2 — an operator address that collides with
    the pending ``SLOT`` suggestion on the same VLAN, or two operator
    ports given the same address, must be caught in the pre-flight —
    neither candidate exists in the database yet for
    ``_validate_static_address()``'s own uniqueness check to catch it
    against, so without this the pre-flight (and the admin form) would
    pass and materialization would fail partway through on the second
    port instead.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")

    def test_operator_address_colliding_with_slot_suggestion_rejected(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="Self Collision Slot", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Dante Primary", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
        )
        # Dante Primary (SLOT, offset 0) at rack_slot 5 would suggest
        # 10.201.6.5 — hand the operator port that exact same address.
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(  # type: ignore[misc]
                device_type=device_type,
                rack=self.rack,
                rack_slot=5,
                operator_addresses={"Device Control": "10.201.6.5"},
            )
        self.assertEqual(NetworkDevicePort.objects.count(), 0)  # nothing written

    def test_two_operator_ports_given_same_address_rejected(self) -> None:
        device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="Self Collision Operator", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control A",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Device Control B",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
        )
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(  # type: ignore[misc]
                device_type=device_type,
                rack=self.rack,
                rack_slot=5,
                operator_addresses={
                    "Device Control A": "10.201.6.4",
                    "Device Control B": "10.201.6.4",
                },
            )
        self.assertEqual(NetworkDevicePort.objects.count(), 0)  # nothing written


class HostnameSuffixLockExemptionTests(TestCase):
    """ADR 0022 decision 4 — ``hostname_suffix`` is exempt from the
    type-port profile lock; every other field on the same row stays
    frozen once the type has instances. Proven both at the model layer
    and through the admin inline (review note 9's
    ``has_change_permission()`` relaxation) — the admin used to return
    ``False`` outright once a profile was locked, which would have made
    the model-level exemption unreachable through the UI.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = _make_device_type(port_count=1, vlan=self.vlan, name="Suffix Lock")
        NetworkDevice.objects.create(device_type=self.device_type)  # locks the profile
        self.type_port = self.device_type.type_ports.get()

    def test_hostname_suffix_editable_once_locked(self) -> None:
        self.type_port.hostname_suffix = "engine"
        self.type_port.save()  # must not raise
        self.type_port.refresh_from_db()
        self.assertEqual(self.type_port.hostname_suffix, "engine")

    def test_other_field_still_refused_once_locked(self) -> None:
        self.type_port.description = "Renamed"
        with self.assertRaises(ValidationError):
            self.type_port.save()

    def test_hostname_suffix_paired_with_another_field_still_refused(self) -> None:
        """The exemption compares against the *persisted* row — a write
        can't smuggle another field's edit through by also touching
        ``hostname_suffix`` in the same ``save()`` (plan Risks section:
        this is the first exemption the profile lock has ever had).
        """
        self.type_port.hostname_suffix = "engine"
        self.type_port.description = "Renamed"
        with self.assertRaises(ValidationError):
            self.type_port.save()
        self.type_port.refresh_from_db()
        self.assertEqual(self.type_port.hostname_suffix, "")

    def test_admin_inline_permits_change_once_locked(self) -> None:
        inline = NetworkDeviceTypePortInline(NetworkDeviceType, AdminSite())
        request = RequestFactory().get("/")
        request.user = User.objects.create_superuser(username="suffixadmin", password="x", email="a@b.c")
        self.assertTrue(inline.has_change_permission(request, self.device_type))

    def test_admin_inline_marks_every_field_but_hostname_suffix_readonly(self) -> None:
        inline = NetworkDeviceTypePortInline(NetworkDeviceType, AdminSite())
        readonly = inline.get_readonly_fields(RequestFactory().get("/"), self.device_type)
        self.assertNotIn("hostname_suffix", readonly)
        for field in ["port_number", "description", "port_type", "vlan", "slot_offset", "address_source"]:
            self.assertIn(field, readonly)

    def test_admin_inline_add_and_delete_still_refused_once_locked(self) -> None:
        inline = NetworkDeviceTypePortInline(NetworkDeviceType, AdminSite())
        request = RequestFactory().get("/")
        self.assertFalse(inline.has_add_permission(request, self.device_type))
        self.assertFalse(inline.has_delete_permission(request, self.device_type))

    def test_admin_inline_readonly_before_any_instance(self) -> None:
        unlocked_type = _make_device_type(port_count=1, vlan=self.vlan, name="Suffix Unlocked")
        inline = NetworkDeviceTypePortInline(NetworkDeviceType, AdminSite())
        readonly = inline.get_readonly_fields(RequestFactory().get("/"), unlocked_type)
        self.assertEqual(readonly, [])

    def test_created_by_change_via_update_fields_still_refused(self) -> None:
        """Codex review of PR 1, P1 — the original comparison hand-listed
        eight fields and silently omitted ``created_by``/``created_at``,
        which let exactly this write (``update_fields=["created_by"]``, no
        ``hostname_suffix`` involved at all) slip through the lock. Every
        concrete field is compared now, introspected from
        ``_meta.concrete_fields`` rather than hand-listed, so a future
        field addition can't reopen this by omission either.
        """
        user = User.objects.create_user("suffixlockuser", password="x")
        self.type_port.created_by = user
        with self.assertRaises(ValidationError):
            self.type_port.save(update_fields=["created_by"])
        self.type_port.refresh_from_db()
        self.assertIsNone(self.type_port.created_by)

    def test_created_at_change_still_refused(self) -> None:
        self.type_port.created_at = timezone.now()
        with self.assertRaises(ValidationError):
            self.type_port.save(update_fields=["created_at"])

    def test_update_fields_hostname_suffix_only_ignores_dirty_other_field(self) -> None:
        """``update_fields`` is honored going forward too, not just as an
        escape route to close: a dirty in-memory ``description`` this
        specific ``save()`` call excludes must not block the
        ``hostname_suffix`` write it *does* include — mirrors
        ``_check_locked_fields_unchanged()``'s own ``update_fields``
        semantics elsewhere in this module.
        """
        self.type_port.hostname_suffix = "engine"
        self.type_port.description = "Renamed"  # dirty, but excluded below
        self.type_port.save(update_fields=["hostname_suffix"])  # must not raise
        self.type_port.refresh_from_db()
        self.assertEqual(self.type_port.hostname_suffix, "engine")
        self.assertEqual(self.type_port.description, "Port 1")  # unchanged — excluded from this write

    def test_queryset_update_bypasses_lock_pre_existing_documented_gap(self) -> None:
        """Not a new gap opened by the ``hostname_suffix`` exemption —
        ``QuerySet.update()`` bypasses ``Model.save()`` (and therefore
        every lock check inside it) for *every* locked field on this
        model, exemption or not; ``_check_locked_fields_unchanged()``
        already documents this as an accepted, unclosed limitation for
        every other locked-field invariant in this module. Recorded here
        as a regression/documentation test — proving the gap is real and
        pre-existing, not something this PR's fix is expected to close.
        """
        NetworkDeviceTypePort.objects.filter(pk=self.type_port.pk).update(description="Renamed via queryset")
        self.type_port.refresh_from_db()
        self.assertEqual(self.type_port.description, "Renamed via queryset")  # bypassed, as documented


class DerivedPortHostnameTests(TestCase):
    """ADR 0022 decision 5 — ``NetworkDevicePort.hostname`` is a derived,
    read-only property: ``<device hostname>-<suffix>`` where both halves
    are non-empty, else ``None`` — deliberately never ``""``, since
    templates test truthiness. Stores nothing.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=4)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.200.1.0/27")

    def _make_suffixed_type(self, **kwargs) -> NetworkDeviceType:
        """SD12-shaped type: a Control port with no suffix, an Engine
        port with ``hostname_suffix="engine"``.
        """
        device_type = NetworkDeviceType.objects.create(
            manufacturer="DiGiCo", model="SD12", port_count=2, **kwargs
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type, description="Control", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        NetworkDeviceTypePort.objects.create(
            device_type=device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            slot_offset=1,
            hostname_suffix="engine",
        )
        return device_type

    def test_hostname_present_when_device_named_and_suffix_set(self) -> None:
        device_type = self._make_suffixed_type(name="Named")
        device = NetworkDevice.objects.create(
            device_type=device_type, rack=self.rack, rack_slot=1, hostname="sd12-96-1"
        )
        engine = device.ports.get(description="Engine")
        self.assertEqual(engine.hostname, "sd12-96-1-engine")

    def test_hostname_none_when_device_unnamed(self) -> None:
        device_type = self._make_suffixed_type(name="Unnamed")
        device = NetworkDevice.objects.create(
            device_type=device_type, rack=self.rack, rack_slot=1, hostname=""
        )
        engine = device.ports.get(description="Engine")
        self.assertIsNone(engine.hostname)

    def test_hostname_none_when_suffix_blank(self) -> None:
        device_type = self._make_suffixed_type(name="No Suffix")
        device = NetworkDevice.objects.create(
            device_type=device_type, rack=self.rack, rack_slot=1, hostname="sd12-96-1"
        )
        control = device.ports.get(description="Control")
        self.assertIsNone(control.hostname)


class OperatorAddressAddFormTests(TestCase):
    """ADR 0022 — ``NetworkDeviceAddForm.__init__`` adds one
    ``GenericIPAddressField`` per ``OPERATOR`` type port on the chosen
    type, and ``clean()``/``_post_clean()`` assembles them into
    ``self.instance.operator_addresses``.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="DM7C Form", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Dante Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
        )
        self.device_control = NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )

    def _data(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "device_type": str(self.device_type.pk),
            "hostname": "dm7c-1",
            "rack": str(self.rack.pk),
            "rack_slot": "5",
            "port_addressing": "static",
            f"operator_address__{self.device_control.pk}": "10.201.6.4",
        }
        data.update(overrides)
        return data

    def test_operator_field_present_for_chosen_type(self) -> None:
        form = NetworkDeviceAddForm(data=self._data())
        field_name = f"operator_address__{self.device_control.pk}"
        self.assertIn(field_name, form.fields)
        self.assertEqual(form.fields[field_name].label, "Device Control")

    def test_no_operator_fields_on_a_bare_unbound_form(self) -> None:
        form = NetworkDeviceAddForm()
        self.assertEqual(form._operator_type_ports, [])
        self.assertNotIn(f"operator_address__{self.device_control.pk}", form.fields)

    def test_valid_submission_assembles_operator_addresses(self) -> None:
        form = NetworkDeviceAddForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.operator_addresses, {"Device Control": "10.201.6.4"})

    def test_missing_operator_address_is_a_form_error(self) -> None:
        field_name = f"operator_address__{self.device_control.pk}"
        data = self._data()
        del data[field_name]
        form = NetworkDeviceAddForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn(field_name, form.errors)

    def test_saved_device_materializes_operator_address_from_form(self) -> None:
        form = NetworkDeviceAddForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)
        device = form.save()
        port = device.ports.get(description="Device Control")
        self.assertEqual(port.address, "10.201.6.4")


class OperatorAddressAdminViewTests(TestCase):
    """Codex review of PR 1, P1 — the operator-address field must actually
    *render* in, and be usable through, the real admin add view, not just
    exist on the form class in isolation (``OperatorAddressAddFormTests``
    above proves the form class works standalone; this proves the admin
    view actually wires it up). Django's ``ModelAdmin`` computes the
    rendered fieldset from the form *class*'s ``base_fields`` — set once
    when ``modelform_factory()`` builds the class, before any instance's
    ``__init__`` ever runs — so a field only ever added inside ``__init__``
    is real for validation but invisible on the page.
    ``NetworkDeviceAddForm.with_operator_fields()``, wired through
    ``NetworkDeviceAdmin.get_form()``, is what fixes that.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.admin_user = User.objects.create_user("opadmin", password="testpass123", is_staff=True)
        self.admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="opadmin", password="testpass123")
        self.vlan = VLAN.objects.create(name="Dante Primary", vlan_id=201, subnet="10.201.0.0/21")
        self.rack = Rack.objects.create(name="Rack 1", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.201.6.0/27")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Yamaha", model="DM7C", name="DM7C Admin View", port_count=2
        )
        NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Dante Primary",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
        )
        self.device_control = NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Device Control",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            address_source=PortAddressSource.OPERATOR,
            hostname_suffix="device-control",
        )

    def test_get_renders_operator_field_for_chosen_type(self) -> None:
        response = self.client.get(f"/admin/inventory/networkdevice/add/?device_type={self.device_type.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'name="operator_address__{self.device_control.pk}"')

    def test_get_omits_operator_field_when_no_type_chosen(self) -> None:
        response = self.client.get("/admin/inventory/networkdevice/add/")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "operator_address__")

    def test_get_omits_operator_field_for_a_type_with_none(self) -> None:
        plain_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Plain", name="Plain Admin View", port_count=1
        )
        NetworkDeviceTypePort.objects.create(
            device_type=plain_type, description="Only Port", port_type=PortType.GBE_RJ45, vlan=self.vlan
        )
        response = self.client.get(f"/admin/inventory/networkdevice/add/?device_type={plain_type.pk}")
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "operator_address__")

    def test_post_creates_device_with_operator_address(self) -> None:
        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": self.device_type.pk,
                "hostname": "dm7c-1",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "5",
                "port_addressing": PortAddressing.STATIC,
                f"operator_address__{self.device_control.pk}": "10.201.6.4",
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        device = NetworkDevice.objects.get(hostname="dm7c-1")
        port = device.ports.get(description="Device Control")
        self.assertEqual(port.address, "10.201.6.4")

    def test_post_missing_operator_address_reports_field_error_not_silent_failure(self) -> None:
        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": self.device_type.pk,
                "hostname": "dm7c-2",
                "serial_number": "",
                "rack": self.rack.pk,
                "rack_slot": "6",
                "port_addressing": PortAddressing.STATIC,
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with a field error, not a 302
        self.assertContains(response, "required for a device that will get a static address")
        self.assertFalse(NetworkDevice.objects.filter(hostname="dm7c-2").exists())

    def test_post_unracked_device_does_not_require_operator_address(self) -> None:
        """Codex review of PR 1, P2 — the field must not block a device
        that will never actually materialize it (an unracked device
        always goes DHCP, ADR 0013 decision 3).
        """
        response = self.client.post(
            "/admin/inventory/networkdevice/add/",
            {
                "device_type": self.device_type.pk,
                "hostname": "dm7c-spare",
                "serial_number": "",
                "rack": "",
                "rack_slot": "",
                "port_addressing": PortAddressing.STATIC,
                "ports-TOTAL_FORMS": "0",
                "ports-INITIAL_FORMS": "0",
                "ports-MIN_NUM_FORMS": "0",
                "ports-MAX_NUM_FORMS": "1000",
            },
        )
        errors = response.context["adminform"].errors if response.context else None
        self.assertEqual(response.status_code, 302, errors)
        device = NetworkDevice.objects.get(hostname="dm7c-spare")
        port = device.ports.get(description="Device Control")
        self.assertTrue(port.is_dhcp)
        self.assertIsNone(port.address)


class ValidateDnsLabelTests(TestCase):
    """ADR 0022 decision 4 (shipped) / ADR 0023 decision 8 (the 63-cap) —
    ``validate_dns_label``'s spec exactly: ``^[a-z0-9]([a-z0-9-]*[a-z0-9])?$``,
    max 63 characters.
    """

    def test_accepts_valid_labels(self) -> None:
        for value in ["engine", "device-control", "a", "a1-2b", "x" * 63]:
            validate_dns_label(value)  # must not raise

    def test_rejects_uppercase(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("Engine")

    def test_rejects_leading_hyphen(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("-engine")

    def test_rejects_trailing_hyphen(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("engine-")

    def test_rejects_blank(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("")

    def test_rejects_over_63_chars(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("x" * 64)

    def test_rejects_underscore(self) -> None:
        with self.assertRaises(ValidationError):
            validate_dns_label("device_control")


class HostnameSuffixNormalizationTests(TestCase):
    """ADR 0022 decision 4 — ``hostname_suffix`` is stripped and
    lowercased in ``clean_fields()``, ``clean()`` and ``save()`` alike, so
    a direct ``objects.create()`` (which never calls ``clean()``) still
    normalizes, and so does ``full_clean()`` (which would otherwise
    validate the raw value before ``clean()`` ever ran — Codex review of
    PR 1, P2).
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Control", vlan_id=200, subnet="10.200.0.0/21")
        self.device_type = NetworkDeviceType.objects.create(
            manufacturer="Test", model="Norm", name="Norm", port_count=1
        )

    def test_suffix_lowercased_and_stripped_on_save(self) -> None:
        type_port = NetworkDeviceTypePort.objects.create(
            device_type=self.device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            hostname_suffix="  Engine  ",
        )
        type_port.refresh_from_db()
        self.assertEqual(type_port.hostname_suffix, "engine")

    def test_suffix_normalized_by_clean_directly(self) -> None:
        """``clean()``'s own normalization, called directly — not through
        ``full_clean()``.
        """
        type_port = NetworkDeviceTypePort(
            device_type=self.device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            hostname_suffix="  Engine  ",
        )
        type_port.clean()
        self.assertEqual(type_port.hostname_suffix, "engine")

    def test_suffix_normalized_by_full_clean(self) -> None:
        """Codex review of PR 1, P2 — ``Model.full_clean()`` runs
        ``clean_fields()`` (which validates ``hostname_suffix`` against
        its own ``validate_dns_label`` field validator) *before* it calls
        ``clean()`` (Django's own ordering). The original version
        normalized only inside ``clean()``, so ``full_clean()`` on
        mixed-case/whitespace input validated the *raw* value and failed
        before ``clean()`` ever got a chance to strip/lowercase it —
        contradicting this field's documented contract (``"MPS "`` stores
        ``"mps"`` rather than erroring). ``clean_fields()`` is now
        overridden to normalize first, so ``full_clean()`` on this input
        succeeds and stores the normalized form, matching
        ``validate_dns_label``'s spec exactly.
        """
        type_port = NetworkDeviceTypePort(
            device_type=self.device_type,
            description="Engine",
            port_type=PortType.GBE_RJ45,
            vlan=self.vlan,
            hostname_suffix="  Engine  ",
        )
        type_port.full_clean()  # must not raise
        self.assertEqual(type_port.hostname_suffix, "engine")


# ---------------------------------------------------------------------------
# ADR 0022 PR 3 — Add-in cards
# ---------------------------------------------------------------------------


class AddInCardModelTests(TestCase):
    """Lifecycle and the three edges ADR 0022 decision 6 draws around
    ``NetworkDevice.host``.
    """

    def setUp(self) -> None:
        self.vlan = VLAN.objects.create(name="Card Dante", vlan_id=810, subnet="10.210.0.0/24")
        self.rack = Rack.objects.create(name="Card Rack", slot_count=20)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.210.0.0/27")
        self.other_rack = Rack.objects.create(name="Other Card Rack", slot_count=20)
        # Carved from the *same* /24 VLAN subnet as self.rack's own range
        # (10.210.0.0/27) — a different, non-overlapping block within it,
        # not a different subnet entirely.
        RackVlanRange.objects.create(rack=self.other_rack, vlan=self.vlan, address_range="10.210.0.32/27")
        self.host_type = _make_device_type(name="Card Host Type")
        self.card_type = _make_device_type(
            port_count=1, vlan=self.vlan, name="Card Type", is_add_in_card=True
        )
        self.host = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=1, hostname="host1"
        )
        self.card = NetworkDevice.objects.create(
            device_type=self.card_type, rack=self.rack, rack_slot=2, hostname="card1"
        )

    # -- Lifecycle --

    def test_fit_then_pull_keeps_rack_slot_and_address(self) -> None:
        original_address = self.card.ports.get().address

        self.card.host = self.host
        self.card.save()
        self.card.refresh_from_db()
        self.assertEqual(self.card.host_id, self.host.pk)
        self.assertEqual(self.card.rack_id, self.rack.pk)
        self.assertEqual(self.card.rack_slot, 2)
        self.assertEqual(self.card.ports.get().address, original_address)

        self.card.host = None
        self.card.save()
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)
        self.assertEqual(self.card.rack_id, self.rack.pk)
        self.assertEqual(self.card.rack_slot, 2)
        self.assertEqual(self.card.ports.get().address, original_address)

    def test_pulled_card_refit_to_a_different_host_is_the_same_row(self) -> None:
        pk = self.card.pk
        self.card.host = self.host
        self.card.save()
        self.card.host = None
        self.card.save()

        other_host = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=3, hostname="host2"
        )
        self.card.host = other_host
        self.card.save()
        self.card.refresh_from_db()
        self.assertEqual(self.card.pk, pk)
        self.assertEqual(self.card.host_id, other_host.pk)

    def test_deleting_host_leaves_card_racked_addressed_and_hostless(self) -> None:
        self.card.host = self.host
        self.card.save()
        address = self.card.ports.get().address

        self.host.delete()

        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)
        self.assertEqual(self.card.rack_id, self.rack.pk)
        self.assertEqual(self.card.rack_slot, 2)
        self.assertEqual(self.card.ports.get().address, address)

    # -- Edges --

    def test_non_card_type_cannot_be_given_a_host_via_full_clean(self) -> None:
        ordinary = NetworkDevice(
            device_type=self.host_type, rack=self.rack, rack_slot=4, hostname="ordinary", host=self.host
        )
        with self.assertRaises(ValidationError):
            ordinary.full_clean()

    def test_non_card_type_cannot_be_given_a_host_via_create(self) -> None:
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(
                device_type=self.host_type, rack=self.rack, rack_slot=4, hostname="ordinary", host=self.host
            )

    def test_card_cannot_host_a_card_via_full_clean(self) -> None:
        second_card = NetworkDevice.objects.create(
            device_type=self.card_type, rack=self.rack, rack_slot=5, hostname="card2"
        )
        second_card.host = self.card
        with self.assertRaises(ValidationError):
            second_card.full_clean()

    def test_card_cannot_host_a_card_via_create(self) -> None:
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(
                device_type=self.card_type, rack=self.rack, rack_slot=5, hostname="card2", host=self.card
            )

    def test_existing_card_cannot_be_set_to_host_itself_via_full_clean(self) -> None:
        self.card.host = self.card
        with self.assertRaises(ValidationError):
            self.card.full_clean()

    def test_existing_card_cannot_be_set_to_host_itself_via_save(self) -> None:
        # No DB CheckConstraint backs this one (see NetworkDevice.Meta's own
        # comment) — MariaDB refuses any CHECK referencing an AUTO_INCREMENT
        # column, verified directly against this project's database. save()
        # is therefore the backstop that must catch this, not just clean().
        self.card.host = self.card
        with self.assertRaises(ValidationError):
            self.card.save()

    def test_self_host_guard_catches_an_explicitly_keyed_row(self) -> None:
        """Codex review, P2 — ``objects.create(pk=X, host_id=X, ...)``
        resolves ``self.host`` against a row that doesn't exist *yet* (this
        instance hasn't been inserted), so ``_get_related()`` catches the
        resulting ``DoesNotExist`` and returns ``None``. The old check
        compared ``host.pk`` (dereferencing first) and so bailed out at
        "host is None" before ever comparing IDs, letting a self-referencing
        row insert. This matters more than an ordinary edge case: MariaDB
        won't take the ``CheckConstraint`` the plan called for, so this
        ``save()``-path guard is the *only* enforcement of the self-host
        edge that exists.
        """
        forced_pk = 1_000_000
        self.assertFalse(NetworkDevice.objects.filter(pk=forced_pk).exists())
        with self.assertRaises(ValidationError):
            NetworkDevice.objects.create(
                pk=forced_pk, device_type=self.card_type, host_id=forced_pk, hostname="self-hosted"
            )
        self.assertFalse(NetworkDevice.objects.filter(pk=forced_pk).exists())

    def test_card_in_a_different_rack_from_its_host_is_allowed(self) -> None:
        card_elsewhere = NetworkDevice.objects.create(
            device_type=self.card_type,
            rack=self.other_rack,
            rack_slot=1,
            hostname="card-elsewhere",
            host=self.host,
        )
        card_elsewhere.full_clean()  # must not raise
        self.assertEqual(card_elsewhere.host_id, self.host.pk)

    def test_hostless_card_is_legal(self) -> None:
        self.assertIsNone(self.card.host_id)
        self.card.full_clean()  # must not raise

    def test_is_add_in_card_cannot_be_changed_once_instances_exist(self) -> None:
        self.card_type.is_add_in_card = False
        with self.assertRaises(ValidationError):
            self.card_type.save()
        with self.assertRaises(ValidationError):
            self.card_type.full_clean()

    def test_is_add_in_card_editable_before_any_instance_exists(self) -> None:
        fresh_type = NetworkDeviceType.objects.create(
            manufacturer="Fresh", model="Type", name="Default", port_count=0
        )
        fresh_type.is_add_in_card = True
        fresh_type.full_clean()
        fresh_type.save()  # must not raise — no instances yet
        fresh_type.refresh_from_db()
        self.assertTrue(fresh_type.is_add_in_card)


class AddInCardAdminReadonlyTests(TestCase):
    """The admin-layer half of the ``is_add_in_card`` lock — mirrors
    ``PortProfileLockedFieldTests``'s pattern for the model-level guard.
    """

    def setUp(self) -> None:
        self.card_type = NetworkDeviceType.objects.create(
            manufacturer="RO", model="Card", name="Default", port_count=0, is_add_in_card=True
        )

    def test_is_add_in_card_readonly_once_instances_exist(self) -> None:
        NetworkDevice.objects.create(device_type=self.card_type)
        admin = NetworkDeviceTypeAdmin(NetworkDeviceType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), self.card_type)
        self.assertIn("is_add_in_card", readonly)

    def test_is_add_in_card_editable_with_no_instances(self) -> None:
        admin = NetworkDeviceTypeAdmin(NetworkDeviceType, AdminSite())
        readonly = admin.get_readonly_fields(RequestFactory().get("/"), self.card_type)
        self.assertNotIn("is_add_in_card", readonly)


def _host_cleared_entry(card_pk: int) -> LogEntry | None:
    """The most recent ``LogEntry`` on ``card_pk`` whose diff names
    ``host`` — used by the audit tests below to find the entry that
    proves the clearing was audited on the *card's own* history, not
    merely "some entry exists somewhere" (which would pass vacuously on
    the host's own deletion record).
    """
    entries = LogEntry.objects.filter(object_pk=str(card_pk), action=LogEntry.Action.UPDATE).order_by(
        "-timestamp", "-pk"
    )
    for entry in entries:
        if "host" in entry.changes_dict:
            return entry
    return None


class AddInCardAuditTests(TestCase):
    """ADR 0022 PR 3's Audit section — the ``SET_NULL`` path does not audit
    itself. Every assertion here checks the *specific* ``host: <pk> ->
    None`` transition with its actor, not merely that some LogEntry exists.
    """

    def setUp(self) -> None:
        self.host_type = _make_device_type(name="Audit Host Type")
        self.card_type = _make_device_type(name="Audit Card Type", is_add_in_card=True)
        self.rack = Rack.objects.create(name="Audit Card Rack", slot_count=10)
        self.host = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=1, hostname="audit-host"
        )
        self.card = NetworkDevice.objects.create(
            device_type=self.card_type, rack=self.rack, rack_slot=2, hostname="audit-card"
        )
        self.actor = User.objects.create_user("card-auditor", password="testpass123")

    def _assert_host_cleared(self, card_pk: int, old_host_pk: int, actor: Any) -> None:
        entry = _host_cleared_entry(card_pk)
        self.assertIsNotNone(entry, "no LogEntry on the card's own history recorded the host change")
        assert entry is not None  # narrows the type for mypy; asserted above
        old_val, new_val = entry.changes_dict["host"]
        self.assertEqual(old_val, str(old_host_pk))
        self.assertEqual(new_val, "None")
        self.assertEqual(entry.actor, actor)

    def test_fitting_is_logged_on_the_card_with_actor(self) -> None:
        with set_actor(actor=self.actor):
            self.card.host = self.host
            self.card.save()
        entry = LogEntry.objects.filter(object_pk=str(self.card.pk), action=LogEntry.Action.UPDATE).latest(
            "timestamp"
        )
        self.assertIn("host", entry.changes_dict)
        old_val, new_val = entry.changes_dict["host"]
        self.assertEqual(old_val, "None")
        self.assertEqual(new_val, str(self.host.pk))
        self.assertEqual(entry.actor, self.actor)

    def test_pulling_is_logged_on_the_card_with_actor(self) -> None:
        self.card.host = self.host
        self.card.save()
        with set_actor(actor=self.actor):
            self.card.host = None
            self.card.save()
        self._assert_host_cleared(self.card.pk, self.host.pk, self.actor)

    def test_host_instance_delete_clears_and_logs_on_the_card(self) -> None:
        self.card.host = self.host
        self.card.save()
        host_pk = self.host.pk  # captured before delete() clears self.host.pk in-place
        with set_actor(actor=self.actor):
            self.host.delete()
        self._assert_host_cleared(self.card.pk, host_pk, self.actor)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)

    def test_host_queryset_delete_clears_and_logs_on_the_card(self) -> None:
        self.card.host = self.host
        self.card.save()
        with set_actor(actor=self.actor):
            NetworkDevice.objects.filter(pk=self.host.pk).delete()
        self._assert_host_cleared(self.card.pk, self.host.pk, self.actor)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)

    def test_host_admin_bulk_delete_action_clears_and_logs_on_the_card_with_actor(self) -> None:
        self.card.host = self.host
        self.card.save()
        call_command("sync_roles", stdout=io.StringIO())
        admin_user = User.objects.create_user("card-audit-admin", password="testpass123", is_staff=True)
        admin_user.groups.add(Group.objects.get(name="Admin"))
        self.client.login(username="card-audit-admin", password="testpass123")

        # Django's delete_selected is a two-step confirm; the actual bulk
        # QuerySet.delete() only fires on the second POST ("post": "yes").
        self.client.post(
            "/admin/inventory/networkdevice/",
            {"action": "delete_selected", "_selected_action": [str(self.host.pk)]},
        )
        response = self.client.post(
            "/admin/inventory/networkdevice/",
            {"action": "delete_selected", "_selected_action": [str(self.host.pk)], "post": "yes"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(NetworkDevice.objects.filter(pk=self.host.pk).exists())

        self._assert_host_cleared(self.card.pk, self.host.pk, admin_user)
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)


class AddInCardConcurrencyTests(TransactionTestCase):
    """ADR 0022 PR 3's Concurrency section. ``TransactionTestCase``, not
    ``TestCase`` — a plain ``TestCase`` wraps everything in one transaction
    and cannot exercise a real cross-connection lock wait.
    """

    def setUp(self) -> None:
        self.host_type = _make_device_type(name="Concurrency Host Type")
        self.card_type = _make_device_type(name="Concurrency Card Type", is_add_in_card=True)
        self.rack = Rack.objects.create(name="Concurrency Card Rack", slot_count=10)
        self.host_a = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=1, hostname="conc-host-a"
        )
        self.host_b = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=2, hostname="conc-host-b"
        )
        self.card = NetworkDevice.objects.create(
            device_type=self.card_type, rack=self.rack, rack_slot=3, hostname="conc-card"
        )

    @staticmethod
    def _locked_fit(
        key: str,
        host_pk: int,
        card_pk: int,
        results: dict[str, str],
        *,
        hold_time: float = 0.0,
        on_locked: Any = None,
    ) -> None:
        """The same locking discipline ``NetworkDeviceAdmin._fit_existing_
        card`` uses: lock host and card together, in ascending pk order,
        then re-check inside the lock.
        """
        try:
            with transaction.atomic():
                locked = _lock_devices_by_pk(host_pk, card_pk)
                if on_locked is not None:
                    on_locked()
                if hold_time:
                    time.sleep(hold_time)
                host = locked.get(host_pk)
                card = locked.get(card_pk)
                if host is None or card is None:
                    results[key] = "gone"
                    return
                if host.device_type.is_add_in_card or not card.device_type.is_add_in_card:
                    results[key] = "invalid"
                    return
                if card.host_id is not None:
                    results[key] = "already-fitted"
                    return
                card.host = host
                card.save()
                results[key] = "fitted"
        finally:
            connection.close()

    @staticmethod
    def _real_host_delete(key: str, host: NetworkDevice, results: dict[str, str]) -> None:
        """Calls the real, unmodified ``NetworkDevice.delete()`` — no
        external pre-lock. An earlier version of this helper locked the
        host itself before calling ``delete()``, which happened to
        serialize things correctly regardless of whether the production
        ``pre_delete`` receiver locked before discovering children, masking
        exactly the ordering bug Codex review found (P1): the receiver used
        to read the card list with a plain, unlocked query *before* taking
        any lock. Every concurrency test below now drives ``delete()``
        directly, so a regression of that ordering fails these tests again.
        """
        try:
            host.delete()
            results[key] = "deleted"
        except Exception as exc:  # noqa: BLE001 — recorded, not swallowed
            results[key] = f"error:{exc}"
        finally:
            connection.close()

    def test_competing_fits_resolve_to_a_single_host(self) -> None:
        """Two requests each observing the card as hostless and fitting it
        to different hosts must not resolve to last-write-wins — one wins,
        the other observes the card already fitted once it acquires its
        own lock.
        """
        lock_acquired = threading.Event()
        results: dict[str, str] = {}

        slow = threading.Thread(
            target=self._locked_fit,
            args=("a", self.host_a.pk, self.card.pk, results),
            kwargs={"hold_time": 0.4, "on_locked": lock_acquired.set},
        )

        def start_fast() -> None:
            lock_acquired.wait(timeout=5)
            self._locked_fit("b", self.host_b.pk, self.card.pk, results)

        fast = threading.Thread(target=start_fast)
        slow.start()
        fast.start()
        slow.join(timeout=10)
        fast.join(timeout=10)

        self.assertEqual(sorted(results.values()), ["already-fitted", "fitted"])
        self.card.refresh_from_db()
        self.assertIn(self.card.host_id, (self.host_a.pk, self.host_b.pk))

    def test_fit_racing_a_host_deletion_neither_orphans_nor_fails_the_delete(self) -> None:
        """The delete wins the race (holds the lock first): the competing
        fit must see the host is already gone and refuse cleanly — never
        silently fit the card to a host that no longer exists, and never
        cause the delete itself to fail on a newly created FK.

        Drives the real ``host.delete()`` call, timed via an instrumented
        ``_lock_devices_by_pk`` (patched only inside ``inventory.models``,
        where the ``pre_delete`` receiver calls it — the fit thread's own
        separately-imported reference is untouched) rather than an external
        pre-lock, so this exercises the production locking order rather
        than a test-only stand-in for it.
        """
        lock_acquired = threading.Event()
        results: dict[str, str] = {}
        real_lock = inventory_models._lock_devices_by_pk

        def instrumented_lock(*pks: int) -> dict[int, NetworkDevice]:
            locked = real_lock(*pks)
            if pks == (self.host_a.pk,):
                lock_acquired.set()
                time.sleep(0.4)
            return locked

        def run_delete() -> None:
            with mock.patch.object(inventory_models, "_lock_devices_by_pk", side_effect=instrumented_lock):
                self._real_host_delete("delete", self.host_a, results)

        def run_fit() -> None:
            lock_acquired.wait(timeout=5)
            self._locked_fit("fit", self.host_a.pk, self.card.pk, results)

        deleter = threading.Thread(target=run_delete)
        fitter = threading.Thread(target=run_fit)
        deleter.start()
        fitter.start()
        deleter.join(timeout=10)
        fitter.join(timeout=10)

        self.assertEqual(results.get("delete"), "deleted")
        self.assertEqual(results.get("fit"), "gone")
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)
        self.assertFalse(NetworkDevice.objects.filter(pk=self.host_a.pk).exists())

    def test_host_deletion_racing_a_fit_clears_the_newly_fitted_card(self) -> None:
        """The other direction: the fit wins the race and commits first —
        the delete must still discover and audited-clear the newly-fitted
        card rather than fail, since it locks before reading which cards
        are fitted. Drives the real ``host.delete()`` call directly (no
        pre-lock) — the fit thread's own held lock is what makes the delete
        thread wait here, exactly as it would under real contention.
        """
        lock_acquired = threading.Event()
        results: dict[str, str] = {}

        fitter = threading.Thread(
            target=self._locked_fit,
            args=("fit", self.host_a.pk, self.card.pk, results),
            kwargs={"hold_time": 0.4, "on_locked": lock_acquired.set},
        )

        def run_delete() -> None:
            lock_acquired.wait(timeout=5)
            self._real_host_delete("delete", self.host_a, results)

        deleter = threading.Thread(target=run_delete)
        fitter.start()
        deleter.start()
        fitter.join(timeout=10)
        deleter.join(timeout=10)

        self.assertEqual(results.get("fit"), "fitted")
        self.assertEqual(results.get("delete"), "deleted")
        self.card.refresh_from_db()
        self.assertIsNone(self.card.host_id)
        self.assertFalse(NetworkDevice.objects.filter(pk=self.host_a.pk).exists())
        entry = _host_cleared_entry(self.card.pk)
        self.assertIsNotNone(entry, "the delete must clear the newly-fitted card through an audited save")


class FitAndPullAdminTests(TestCase):
    """The dedicated "fit a card" view and the Pull action — permissions,
    server-side validation of a crafted POST, and the shape of each write.
    """

    def setUp(self) -> None:
        call_command("sync_roles", stdout=io.StringIO())
        self.vlan = VLAN.objects.create(name="Fit VLAN", vlan_id=813, subnet="10.213.0.0/24")
        self.rack = Rack.objects.create(name="Fit Rack", slot_count=10)
        RackVlanRange.objects.create(rack=self.rack, vlan=self.vlan, address_range="10.213.0.0/27")
        self.host_type = _make_device_type(name="Fit Host Type")
        self.card_type = _make_device_type(name="Fit Card Type", is_add_in_card=True)
        self.non_card_type = _make_device_type(name="Fit Non-card Type")
        self.card_host_type = _make_device_type(name="Fit Card Host Type", is_add_in_card=True)

        self.host = NetworkDevice.objects.create(
            device_type=self.host_type, rack=self.rack, rack_slot=1, hostname="fit-host"
        )
        self.hostless_card = NetworkDevice.objects.create(
            device_type=self.card_type, rack=self.rack, rack_slot=2, hostname="fit-card"
        )
        self.card_typed_host = NetworkDevice.objects.create(
            device_type=self.card_host_type, rack=self.rack, rack_slot=3, hostname="fit-card-as-host"
        )

        self.editor = User.objects.create_user("fit-editor", password="testpass123", is_staff=True)
        self.editor.groups.add(Group.objects.get(name="Editor"))
        self.viewer = User.objects.create_user("fit-viewer", password="testpass123", is_staff=True)
        self.viewer.groups.add(Group.objects.get(name="Viewer"))

        self.add_only = User.objects.create_user("fit-add-only", password="testpass123", is_staff=True)
        self.add_only.user_permissions.add(
            Permission.objects.get(codename="add_networkdevice"),
            Permission.objects.get(codename="view_networkdevice"),
        )
        self.change_only = User.objects.create_user("fit-change-only", password="testpass123", is_staff=True)
        self.change_only.user_permissions.add(
            Permission.objects.get(codename="change_networkdevice"),
            Permission.objects.get(codename="view_networkdevice"),
        )

    def _login(self, user: Any) -> None:
        self.client.force_login(user)

    def _fit_url(self, host: NetworkDevice | None = None) -> str:
        return f"/admin/inventory/networkdevice/{(host or self.host).pk}/fit-card/"

    # -- existing-card path --

    def test_existing_card_path_preserves_pk_and_fits(self) -> None:
        self._login(self.editor)
        pk = self.hostless_card.pk
        response = self.client.post(self._fit_url(), {"fit_mode": "existing", "card": str(pk)})
        self.assertEqual(response.status_code, 302)
        self.hostless_card.refresh_from_db()
        self.assertEqual(self.hostless_card.pk, pk)
        self.assertEqual(self.hostless_card.host_id, self.host.pk)

    def test_existing_card_path_denied_without_change(self) -> None:
        self._login(self.add_only)
        response = self.client.post(
            self._fit_url(), {"fit_mode": "existing", "card": str(self.hostless_card.pk)}
        )
        self.assertEqual(response.status_code, 403)
        self.hostless_card.refresh_from_db()
        self.assertIsNone(self.hostless_card.host_id)

    def test_existing_card_path_denied_for_viewer(self) -> None:
        self._login(self.viewer)
        response = self.client.post(
            self._fit_url(), {"fit_mode": "existing", "card": str(self.hostless_card.pk)}
        )
        self.assertEqual(response.status_code, 403)
        self.hostless_card.refresh_from_db()
        self.assertIsNone(self.hostless_card.host_id)

    def test_crafted_post_naming_a_card_typed_host_is_refused(self) -> None:
        self._login(self.editor)
        response = self.client.post(
            self._fit_url(host=self.card_typed_host),
            {"fit_mode": "existing", "card": str(self.hostless_card.pk)},
        )
        self.assertEqual(response.status_code, 302)
        self.hostless_card.refresh_from_db()
        self.assertIsNone(self.hostless_card.host_id)

    def test_crafted_post_naming_a_non_card_row_as_the_card_is_refused(self) -> None:
        self._login(self.editor)
        non_card_device = NetworkDevice.objects.create(
            device_type=self.non_card_type, rack=self.rack, rack_slot=4, hostname="not-a-card"
        )
        response = self.client.post(
            self._fit_url(), {"fit_mode": "existing", "card": str(non_card_device.pk)}
        )
        self.assertEqual(response.status_code, 302)
        non_card_device.refresh_from_db()
        self.assertIsNone(non_card_device.host_id)

    # -- create path --

    def test_create_path_requires_add_and_change_and_inserts_once_with_host_set(self) -> None:
        self._login(self.editor)
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(self.card_type.pk),
                "hostname": "new-card",
                "rack": str(self.rack.pk),
                "rack_slot": "5",
                "port_addressing": "dhcp",
            },
        )
        self.assertEqual(response.status_code, 302)
        new_card = NetworkDevice.objects.get(hostname="new-card")
        self.assertEqual(new_card.host_id, self.host.pk)
        # Exactly one row for this hostname — construct-once, not an
        # insert followed by a patch (review note 5).
        self.assertEqual(NetworkDevice.objects.filter(hostname="new-card").count(), 1)

    def test_create_path_denied_without_add(self) -> None:
        self._login(self.change_only)
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(self.card_type.pk),
                "hostname": "denied-card-1",
                "rack": str(self.rack.pk),
                "rack_slot": "5",
                "port_addressing": "dhcp",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(NetworkDevice.objects.filter(hostname="denied-card-1").exists())

    def test_create_path_denied_without_change(self) -> None:
        self._login(self.add_only)
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(self.card_type.pk),
                "hostname": "denied-card-2",
                "rack": str(self.rack.pk),
                "rack_slot": "5",
                "port_addressing": "dhcp",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(NetworkDevice.objects.filter(hostname="denied-card-2").exists())

    def test_crafted_post_naming_a_type_outside_the_picker_queryset_is_refused(self) -> None:
        self._login(self.editor)
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(self.non_card_type.pk),
                "hostname": "should-not-exist",
                "rack": str(self.rack.pk),
                "rack_slot": "5",
                "port_addressing": "dhcp",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with a form error
        self.assertFalse(NetworkDevice.objects.filter(hostname="should-not-exist").exists())

    def test_crafted_post_with_a_malformed_device_type_id_is_refused_not_500(self) -> None:
        """Codex review, P2 — a non-numeric ``device_type`` used to reach a
        raw ``QuerySet.filter(device_type_id=<garbage>)`` call inside
        ``with_operator_fields()`` before ``ModelChoiceField`` ever got a
        chance to turn it into an ordinary form error, raising a bare
        ``ValueError`` (a 500) instead.
        """
        self._login(self.editor)
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": "not-an-int",
                "hostname": "should-not-exist-either",
                "rack": str(self.rack.pk),
                "rack_slot": "5",
                "port_addressing": "dhcp",
            },
        )
        self.assertEqual(response.status_code, 200)  # re-rendered with a form error, never a 500
        self.assertFalse(NetworkDevice.objects.filter(hostname="should-not-exist-either").exists())

    def test_fit_card_view_denies_get_for_staff_with_no_device_permissions(self) -> None:
        """Codex review, P2 — ``admin_site.admin_view`` only supplies
        login/staff gating, never a model permission check. Without an
        explicit check, a staff user holding no ``NetworkDevice`` permission
        at all could GET this URL directly and see the host rendered before
        either per-section template guard ever runs.
        """
        no_perms_user = User.objects.create_user("fit-no-perms", password="testpass123", is_staff=True)
        self._login(no_perms_user)
        response = self.client.get(self._fit_url())
        self.assertEqual(response.status_code, 403)

    def test_create_path_prompts_for_operator_port_address(self) -> None:
        """A card type carrying an OPERATOR port must still prompt for its
        address — proof the create path really goes through
        ``NetworkDeviceAddForm.with_operator_fields()`` rather than a
        bespoke form that would silently skip it (review note 5).
        """
        operator_vlan = VLAN.objects.create(name="Fit Operator VLAN", vlan_id=814, subnet="10.214.0.0/24")
        RackVlanRange.objects.create(rack=self.rack, vlan=operator_vlan, address_range="10.214.0.0/27")
        operator_card_type = NetworkDeviceType.objects.create(
            manufacturer="Fit", model="OperatorCard", name="Default", port_count=1, is_add_in_card=True
        )
        type_port = NetworkDeviceTypePort.objects.create(
            device_type=operator_card_type,
            description="Aux",
            port_type=PortType.GBE_RJ45,
            vlan=operator_vlan,
            address_source=PortAddressSource.OPERATOR,
        )
        self._login(self.editor)

        # No operator_address__<pk> supplied — must be refused, not silently
        # materialized DHCP or with a blank address.
        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(operator_card_type.pk),
                "hostname": "operator-card",
                "rack": str(self.rack.pk),
                "rack_slot": "6",
                "port_addressing": "static",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(NetworkDevice.objects.filter(hostname="operator-card").exists())

        response = self.client.post(
            self._fit_url(),
            {
                "fit_mode": "create",
                "device_type": str(operator_card_type.pk),
                "hostname": "operator-card",
                "rack": str(self.rack.pk),
                "rack_slot": "6",
                "port_addressing": "static",
                f"operator_address__{type_port.pk}": "10.214.0.20",
            },
        )
        self.assertEqual(response.status_code, 302)
        card = NetworkDevice.objects.get(hostname="operator-card")
        self.assertEqual(card.host_id, self.host.pk)
        self.assertEqual(card.ports.get().address, "10.214.0.20")

    # -- Pull --

    def test_pull_requires_change(self) -> None:
        self.hostless_card.host = self.host
        self.hostless_card.save()
        self._login(self.viewer)
        response = self.client.post(
            "/admin/inventory/networkdevice/",
            {"action": "pull_cards", "_selected_action": [str(self.hostless_card.pk)]},
        )
        # An action outside the user's permitted set is dropped by Django's
        # own action-permission filtering, not a 403 — the changelist just
        # re-renders with nothing changed.
        self.assertEqual(response.status_code, 200)
        self.hostless_card.refresh_from_db()
        self.assertEqual(self.hostless_card.host_id, self.host.pk)

    def test_pull_clears_host_leaves_rack_and_slot(self) -> None:
        self.hostless_card.host = self.host
        self.hostless_card.save()
        rack_id, slot = self.hostless_card.rack_id, self.hostless_card.rack_slot
        self._login(self.editor)
        response = self.client.post(
            "/admin/inventory/networkdevice/",
            {"action": "pull_cards", "_selected_action": [str(self.hostless_card.pk)]},
        )
        self.assertEqual(response.status_code, 302)
        self.hostless_card.refresh_from_db()
        self.assertIsNone(self.hostless_card.host_id)
        self.assertEqual(self.hostless_card.rack_id, rack_id)
        self.assertEqual(self.hostless_card.rack_slot, slot)
