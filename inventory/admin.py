from django.contrib import admin
from django.forms import BaseModelFormSet
from django.http import HttpRequest

from .models import (
    VLAN,
    AuditedModel,
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


class AuditedModelAdminMixin:
    """Populates ``created_by`` from the request instead of leaving it null.

    ``created_by`` is ``editable=False`` on the model, so it never appears
    as a user-selectable field on any form generated from it — this mixin
    is what actually sets it, both for the admin's own object and for
    child rows added through an inline formset.
    """

    def save_model(self, request: HttpRequest, obj: AuditedModel, form: object, change: bool) -> None:
        if not change:
            # request.user is AnonymousUser | User in general, but the admin
            # enforces authentication before save_model is ever reached.
            obj.created_by = request.user  # type: ignore[assignment]
        super().save_model(request, obj, form, change)  # type: ignore[misc]

    def save_formset(
        self, request: HttpRequest, form: object, formset: BaseModelFormSet, change: bool
    ) -> None:
        instances = formset.save(commit=False)
        for instance in instances:
            if instance.pk is None:
                instance.created_by = request.user
            instance.save()
        formset.save_m2m()


class RackVlanRangeInline(admin.TabularInline):
    model = RackVlanRange
    extra = 0


class NetworkSwitchAddressInline(admin.TabularInline):
    model = NetworkSwitchAddress
    extra = 0


class NetworkSwitchPortInline(admin.TabularInline):
    model = NetworkSwitchPort
    extra = 0


class NetworkDevicePortInline(admin.TabularInline):
    model = NetworkDevicePort
    extra = 0


@admin.register(VLAN)
class VLANAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["name", "vlan_id", "subnet", "default_gateway", "dhcp_range"]
    search_fields = ["name", "vlan_id", "subnet"]
    ordering = ["vlan_id"]


@admin.register(Rack)
class RackAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slot_count"]
    search_fields = ["name"]
    inlines = [RackVlanRangeInline]


@admin.register(NetworkSwitchType)
class NetworkSwitchTypeAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]


@admin.register(NetworkSwitch)
class NetworkSwitchAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["hostname", "switch_type", "serial_number", "rack", "rack_slot", "dhcp_server_enabled"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "switch_type"]
    inlines = [NetworkSwitchAddressInline, NetworkSwitchPortInline]


@admin.register(NetworkDeviceType)
class NetworkDeviceTypeAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]


@admin.register(NetworkDevice)
class NetworkDeviceAdmin(AuditedModelAdminMixin, admin.ModelAdmin):
    list_display = ["hostname", "device_type", "serial_number", "rack", "rack_slot"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "device_type"]
    inlines = [NetworkDevicePortInline]
