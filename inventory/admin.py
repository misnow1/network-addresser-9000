from typing import Any

from auditlog.mixins import AuditlogHistoryAdminMixin
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as default_delete_selected
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.db.models import QuerySet
from django.forms import BaseInlineFormSet, BaseModelFormSet
from django.http import HttpRequest, HttpResponseRedirect
from django.template.response import TemplateResponse

from .models import (
    VLAN,
    AuditedModel,
    NetworkDevice,
    NetworkDevicePort,
    NetworkDeviceType,
    NetworkDeviceTypePort,
    NetworkSwitch,
    NetworkSwitchAddress,
    NetworkSwitchPort,
    NetworkSwitchType,
    NetworkSwitchTypePort,
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

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        """Turns a ``ValidationError``/``IntegrityError`` raised from inside
        ``save_model()``/``save_formset()`` into a redirect-with-message
        instead of an unhandled-exception 500 page.

        Some of this app's invariants (ADR 0010's locked-field and
        profile-lock checks) are enforced inside ``Model.save()``/
        ``delete()`` themselves, not only ``clean()`` — by design, since a
        few of them (row-locking against a concurrent edit, or a
        same-submission ordinal collision) can only be detected at save
        time. Django's admin only turns ``clean()``-raised errors into a
        form error automatically; anything a locked-field guard raises
        later, from ``save()``/``delete()`` itself, would otherwise
        propagate straight past the admin's normal error handling.
        """
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)  # type: ignore[misc]
        except (ValidationError, IntegrityError) as exc:
            messages.error(request, f"Could not save: {exc}")
            return HttpResponseRedirect(request.path)

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


class _PortCountFormSet(BaseInlineFormSet):
    """Shared "the parent's declared port_count must match the number of
    type-port rows defined here" check (see ADR 0010) — the admin-side half
    of the completeness guard; the model-level ``_materialize_ports()``
    backstop (``inventory/models.py``) still applies if this is ever
    bypassed (e.g. a direct API/ORM write).
    """

    #: Subclasses set this to also require the surviving port numbers to be
    #: a contiguous ``1..port_count`` sequence (switch type ports only —
    #: device type ports have no numbering requirement).
    require_contiguous_numbering = False

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        port_count = getattr(self.instance, "port_count", None)
        if not port_count:
            return  # parent itself invalid/unset; its own validation reports that
        surviving = [
            form for form in self.forms if form.cleaned_data and not form.cleaned_data.get("DELETE", False)
        ]
        if len(surviving) != port_count:
            raise forms.ValidationError(
                f"This profile declares port_count {port_count} but {len(surviving)} port(s) are "
                "defined here — define exactly that many before saving."
            )
        if self.require_contiguous_numbering:
            numbers = sorted(
                form.cleaned_data["port_number"] for form in surviving if form.cleaned_data.get("port_number")
            )
            if numbers != list(range(1, port_count + 1)):
                raise forms.ValidationError(
                    f"Port numbers must be a contiguous 1..{port_count} sequence (found {numbers})."
                )


class NetworkSwitchTypePortFormSet(_PortCountFormSet):
    require_contiguous_numbering = True


class NetworkDeviceTypePortFormSet(_PortCountFormSet):
    pass


class NetworkSwitchTypePortForm(forms.ModelForm):
    """``allowed_vlans`` uses an explicit through model (ADR 0010, so a VLAN
    still referenced by it can't be deleted out from under a profile) —
    Django's admin can't auto-generate a widget for a ManyToManyField with a
    non-auto-created through model, so it's declared here by hand instead.
    The through model has no fields beyond the two FKs, so the model's own
    ``.set()`` (invoked by ``ModelForm``'s normal ``save_m2m()``) still
    works exactly like a plain M2M would.
    """

    allowed_vlans = forms.ModelMultipleChoiceField(queryset=VLAN.objects.all(), required=False)

    class Meta:
        model = NetworkSwitchTypePort
        # switch_type (the parent link) is deliberately omitted — the
        # inline formset sets it directly, not through the form.
        fields = ["port_number", "description", "port_type", "port_mode", "native_vlan", "allowed_vlans"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["allowed_vlans"].initial = self.instance.allowed_vlans.all()


class NetworkSwitchPortForm(forms.ModelForm):
    """See ``NetworkSwitchTypePortForm`` — same reason ``allowed_vlans`` is
    declared by hand here.
    """

    allowed_vlans = forms.ModelMultipleChoiceField(queryset=VLAN.objects.all(), required=False)

    class Meta:
        model = NetworkSwitchPort
        # switch (the parent link) is deliberately omitted — the inline
        # formset sets it directly, not through the form.
        fields = ["port_number", "description", "port_type", "port_mode", "native_vlan", "allowed_vlans"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["allowed_vlans"].initial = self.instance.allowed_vlans.all()


def _profile_locked(type_obj: Any, instances_related_name: str) -> bool:
    """Whether a Network Switch/Device Type's port profile is locked —
    i.e. it already has at least one instance (ADR 0010).
    """
    return (
        type_obj is not None
        and type_obj.pk is not None
        and getattr(type_obj, instances_related_name).exists()
    )


class NetworkSwitchTypePortInline(admin.TabularInline):
    """``fields`` is deliberately left unset: Django's admin checks
    (admin.E013) refuse to let a ManyToManyField with a manually-specified
    through model appear in an explicit ``fields`` list, even though the
    custom ``form`` above declares ``allowed_vlans`` by hand — so this
    falls back to ``form.base_fields`` (which does include it) instead.
    """

    model = NetworkSwitchTypePort
    form = NetworkSwitchTypePortForm
    formset = NetworkSwitchTypePortFormSet
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).prefetch_related("allowed_vlans")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "switches"):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "switches"):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "switches"):
            return False
        return super().has_delete_permission(request, obj)


class NetworkDeviceTypePortInline(admin.TabularInline):
    model = NetworkDeviceTypePort
    formset = NetworkDeviceTypePortFormSet
    extra = 0
    fields = ["port_number", "description", "port_type", "vlan"]

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "devices"):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "devices"):
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "devices"):
            return False
        return super().has_delete_permission(request, obj)


class NetworkSwitchPortInline(admin.TabularInline):
    """Instance ports are materialized from the switch's type on creation
    (ADR 0010), never hand-added or removed — ``port_type`` (a hardware
    fact copied from the type) is locked too; only VLAN purpose stays
    editable.
    """

    model = NetworkSwitchPort
    form = NetworkSwitchPortForm
    # No explicit ``fields`` here either — see NetworkSwitchTypePortInline.
    readonly_fields = ["port_number", "port_type"]
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).prefetch_related("allowed_vlans")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class NetworkDevicePortInline(admin.TabularInline):
    """See ``NetworkSwitchPortInline`` — same materialized-only reasoning.
    ``description``/``vlan``/``port_type`` are locked; only DHCP/address/
    the connected switch port stay editable. ``default_gateway`` is a
    read-only derived property (ADR 0010), shown but never editable.
    """

    model = NetworkDevicePort
    fields = [
        "description",
        "port_number",
        "port_type",
        "vlan",
        "is_dhcp",
        "address",
        "default_gateway",
        "switch_port",
    ]
    readonly_fields = ["description", "port_number", "vlan", "port_type", "default_gateway"]
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # ``vlan`` is readonly-displayed and ``default_gateway`` reads it
        # live per row — without this, both N+1 across a device's ports.
        return super().get_queryset(request).select_related("vlan")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(VLAN)
class VLANAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "vlan_id", "subnet", "default_gateway", "dhcp_range_start", "dhcp_range_end"]
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
    list_display = ["manufacturer", "model", "name", "port_count"]
    search_fields = ["manufacturer", "model", "name"]
    inlines = [NetworkSwitchTypePortInline]
    show_auditlog_history_link = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        if _profile_locked(obj, "switches"):
            return ["manufacturer", "model", "name", "port_count"]
        return []


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

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # switch_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is not None:
            return ["switch_type"]
        return []

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
    list_display = ["manufacturer", "model", "name", "port_count"]
    search_fields = ["manufacturer", "model", "name"]
    inlines = [NetworkDeviceTypePortInline]
    show_auditlog_history_link = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        if _profile_locked(obj, "devices"):
            return ["manufacturer", "model", "name", "port_count"]
        return []


@admin.register(NetworkDevice)
class NetworkDeviceAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["hostname", "device_type", "serial_number", "rack", "rack_slot"]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "device_type"]
    inlines = [NetworkDevicePortInline]
    show_auditlog_history_link = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # device_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is not None:
            return ["device_type"]
        return []
