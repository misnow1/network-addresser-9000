from typing import Any

from auditlog.mixins import AuditlogHistoryAdminMixin
from django.contrib import admin
from django.contrib.admin.actions import delete_selected as default_delete_selected
from django.db.models import QuerySet
from django.forms import BaseModelFormSet
from django.http import HttpRequest
from django.template.response import TemplateResponse

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
        for obj in formset.deleted_objects:
            obj.delete()
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
class VLANAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "vlan_id", "subnet", "default_gateway", "dhcp_range"]
    search_fields = ["name", "vlan_id", "subnet"]
    ordering = ["vlan_id"]
    show_auditlog_history_link = True


@admin.register(Rack)
class RackAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slot_count"]
    search_fields = ["name"]
    inlines = [RackVlanRangeInline]
    show_auditlog_history_link = True


@admin.register(NetworkSwitchType)
class NetworkSwitchTypeAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]
    show_auditlog_history_link = True


def _connected_device_ports(switches: QuerySet) -> list[NetworkDevicePort]:
    """Device ports plugged into any of ``switches``' ports.

    ``NetworkDevicePort.switch_port`` is ``SET_NULL`` (ADR 0007: leaf
    references unassign rather than cascade), so Django's own
    ``get_deleted_objects`` walk — which only lists objects that will
    themselves be deleted — never mentions them. Shared by the single-object
    and bulk delete confirmation flows below.
    """
    return list(
        NetworkDevicePort.objects.filter(switch_port__switch__in=switches).select_related(
            "device", "switch_port"
        )
    )


@admin.action(permissions=["delete"], description="Delete selected network switches")
def delete_selected(modeladmin: "NetworkSwitchAdmin", request: HttpRequest, queryset: QuerySet) -> Any:
    """Shadows the site-wide ``delete_selected`` action (same name, so
    ``ModelAdmin._get_base_actions`` skips the default per Django's
    documented override pattern) to add the same "other devices route
    through it" warning as the single-object delete flow.

    ``permissions=["delete"]`` matches the default action's own metadata
    (``django.contrib.admin.actions.delete_selected``) — without it, this
    replacement has no ``allowed_permissions`` at all, so
    ``_filter_actions_by_permissions`` treats it as unrestricted and offers
    it to Viewers/Editors too (caught by Codex review).
    """
    response = default_delete_selected(modeladmin, request, queryset)
    if isinstance(response, TemplateResponse) and response.context_data is not None:
        response.context_data["connected_device_ports"] = _connected_device_ports(queryset)
    return response


@admin.register(NetworkSwitch)
class NetworkSwitchAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["hostname", "switch_type", "serial_number", "rack", "rack_slot", "dhcp_server_enabled"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "switch_type"]
    inlines = [NetworkSwitchAddressInline, NetworkSwitchPortInline]
    show_auditlog_history_link = True
    actions = [delete_selected]

    def delete_view(
        self, request: HttpRequest, object_id: str, extra_context: dict[str, object] | None = None
    ) -> Any:
        """Surfaces device ports that would be silently unassigned by this delete.

        The "big scary" confirmation template
        (``admin/inventory/delete_confirmation.html``) renders the list this
        adds to ``extra_context`` when present.
        """
        extra_context = dict(extra_context or {})
        switch = self.get_object(request, object_id)
        if switch is not None:
            extra_context["connected_device_ports"] = _connected_device_ports(
                NetworkSwitch.objects.filter(pk=switch.pk)
            )
        return super().delete_view(request, object_id, extra_context)


@admin.register(NetworkDeviceType)
class NetworkDeviceTypeAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["manufacturer", "model", "port_count", "port_type"]
    search_fields = ["manufacturer", "model"]
    show_auditlog_history_link = True


@admin.register(NetworkDevice)
class NetworkDeviceAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["hostname", "device_type", "serial_number", "rack", "rack_slot"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "device_type"]
    inlines = [NetworkDevicePortInline]
    show_auditlog_history_link = True
