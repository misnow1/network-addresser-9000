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
    _PROFILE_IN_USE_LOCKED_FIELDS,
    _PROFILE_SYSTEM_LOCKED_FIELDS,
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
    PortAddressing,
    PortMode,
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchPortVlanProfile,
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


class RackVlanRangeInlineFormSet(BaseInlineFormSet):
    """Implements ADR 0014 decision 11: a manually-entered range here that
    names a VLAN the chosen template already covers is a validation error
    naming that VLAN — not silent precedence in either direction, and not a
    raw ``unique_rack_vlan_range`` IntegrityError surfacing after template
    rows have already been written.

    Reads ``self.instance.template`` — populated by ``RackAddForm.
    _post_clean()`` before this formset's own ``clean()`` runs. This works
    because ``formset.instance`` *is* ``form.instance`` (the same object,
    not a copy) and Django's admin ``_changeform_view`` only calls
    ``all_valid(formsets)`` (which is what triggers formset validation)
    *after* ``form.is_valid()`` has already run ``_post_clean()`` — even
    though the formsets themselves are *constructed* earlier, before
    ``form.is_valid()`` runs. If a future Django release reorders that,
    this degrades to the raw IntegrityError decision 11 forbids rather than
    silently doing nothing — ``RackTemplateAdminTests`` asserts the *form
    error* specifically so that regression fails loudly. On the rack change
    form (no ``template`` field at all), ``self.instance.template`` is
    simply the property's default ``None`` and this check is a no-op,
    exactly as intended.
    """

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        template = getattr(self.instance, "template", None)
        if template is None:
            return
        template_vlan_ids = set(template.vlan_links.values_list("vlan_id", flat=True))
        if not template_vlan_ids:
            return
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE", False):
                continue
            vlan = form.cleaned_data.get("vlan")
            if vlan is not None and vlan.pk in template_vlan_ids:
                raise forms.ValidationError(
                    f"{vlan} is already included by the selected Rack Template — remove this row "
                    "or choose a different VLAN; it will be allocated by the template automatically."
                )


class RackVlanRangeInline(admin.TabularInline):
    model = RackVlanRange
    formset = RackVlanRangeInlineFormSet
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


class SwitchPortVlanProfileForm(forms.ModelForm):
    """Carries the ``allowed_vlans`` invariants that can't live in
    ``SwitchPortVlanProfile.clean()`` (see that model's docstring: no pk yet
    on create, stale M2M on edit since ``save_m2m()`` runs after ``save()``)
    — this is the one enforcement path that can turn them into field-level
    form errors instead of a raised ``ValidationError`` after the fact. The
    other paths (``m2m_changed`` receiver, through-model ``clean()``/
    ``save()``, and the profile's own ``save()`` against persisted links)
    are backstops for non-form writes, not duplicated here.
    """

    allowed_vlans = forms.ModelMultipleChoiceField(queryset=VLAN.objects.all(), required=False)

    class Meta:
        model = SwitchPortVlanProfile
        fields = ["name", "port_mode", "native_vlan", "all_vlans_allowed", "allowed_vlans"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["allowed_vlans"].initial = self.instance.allowed_vlans.all()

    def _effective(self, cleaned_data: dict[str, Any], field: str, default: Any) -> Any:
        """``cleaned_data[field]``, or the instance's current persisted
        value if ``field`` was excluded from this form entirely —
        ``SwitchPortVlanProfileAdmin.get_readonly_fields()`` drops
        ``port_mode``/``native_vlan`` (and, on the system profile,
        ``all_vlans_allowed``) once locked, so they're simply *absent* from
        ``cleaned_data`` rather than present-but-unchanged. Falling back to
        the instance's value is correct precisely because "locked" means
        this submission cannot be changing it anyway. Falls back to
        ``default`` only for a brand-new, not-yet-saved profile, where
        nothing is ever excluded in the first place.
        """
        if field in cleaned_data:
            return cleaned_data[field]
        return getattr(self.instance, field) if self.instance.pk else default

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        if any(self.errors):
            return cleaned_data
        all_vlans_allowed = self._effective(cleaned_data, "all_vlans_allowed", False)
        port_mode = self._effective(cleaned_data, "port_mode", PortMode.TRUNK)
        native_vlan = self._effective(cleaned_data, "native_vlan", None)
        allowed_vlans = cleaned_data.get("allowed_vlans")  # never excluded — always editable
        if port_mode == PortMode.ACCESS and all_vlans_allowed:
            raise forms.ValidationError("all_vlans_allowed cannot be set while port_mode is Access.")
        if allowed_vlans and (all_vlans_allowed or port_mode == PortMode.ACCESS):
            raise forms.ValidationError(
                "allowed_vlans must be empty when all_vlans_allowed is set or port_mode is Access."
            )
        if native_vlan is not None and allowed_vlans and allowed_vlans.filter(pk=native_vlan.pk).exists():
            raise forms.ValidationError(
                "native_vlan is already listed in allowed_vlans — it's implicitly allowed and "
                "must not also be listed explicitly."
            )
        # This form has now validated the *complete* submitted state
        # (scalars together with the new allowed_vlans selection) — grant
        # the one-instance exemption so the model's own persisted-links
        # check (which only ever sees the *old* M2M state; save_m2m() runs
        # after both _post_clean() and save()) doesn't reject a valid
        # combined edit, e.g. enabling all_vlans_allowed while clearing
        # allowed_vlans in the same submission. See
        # SwitchPortVlanProfile._validate_scalars_against_persisted_links.
        self.instance._trust_pending_m2m_from_form = True
        return cleaned_data


class RackTemplateForm(forms.ModelForm):
    """Carries ``vlans`` as a hand-declared ``ModelMultipleChoiceField`` —
    Django admin.E013 blocks a through-model M2M from ``ModelAdmin.fields``
    directly, the same reason ``SwitchPortVlanProfileForm.allowed_vlans``
    exists.

    ``clean_vlans()`` rejects an L2-only VLAN here so the rule surfaces as a
    field error rather than a save-time exception — the model-level guards
    (``RackTemplateVlan.clean()``/``save()``, the ``m2m_changed`` receiver)
    are backstops for non-form writes, not duplicated here.
    """

    vlans = forms.ModelMultipleChoiceField(queryset=VLAN.objects.all(), required=False)

    class Meta:
        model = RackTemplate
        fields = ["name", "slot_count", "vlans"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["vlans"].initial = self.instance.vlans.all()

    def clean_vlans(self) -> QuerySet:
        vlans = self.cleaned_data["vlans"]
        l2_only = vlans.filter(subnet="")
        if l2_only.exists():
            names = ", ".join(str(vlan) for vlan in l2_only)
            raise forms.ValidationError(
                f"{names} — a VLAN with no subnet is L2-only (ADR 0012) and cannot be included in "
                "a Rack Template."
            )
        return vlans


class NetworkSwitchPortForm(forms.ModelForm):
    """Disables ``profile`` on a row that already has a connected device
    port (DESIGN.md: profile can be swapped for another "unless a device is
    already connected"). ``InlineModelAdmin.get_readonly_fields()`` can't
    express this — it receives the parent ``NetworkSwitch``, not each port,
    and its result applies to the whole formset, so it can't lock just the
    connected rows while leaving free ones editable in the same formset.

    ``disabled=True`` (not merely leaving it out of ``readonly_fields``) so
    a crafted POST can't smuggle a new value past it — Django ignores a
    disabled field's submitted data and keeps the form's initial value
    instead. The model-level guard (``NetworkSwitchPort.save()``) still
    stands for direct ORM/API writes that never go through this form.
    """

    class Meta:
        model = NetworkSwitchPort
        # switch (the parent link) is deliberately omitted — the inline
        # formset sets it directly, not through the form.
        fields = ["port_number", "description", "port_type", "profile"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk and hasattr(self.instance, "connected_device_port"):
            self.fields["profile"].disabled = True


class NetworkDeviceAddForm(forms.ModelForm):
    """Carries the creation-time-only ``port_addressing`` choice (ADR 0013)
    — not a model field, so it can't be expressed via ``Meta.fields``
    alone. Used only for the add view (``NetworkDeviceAdmin.get_form()``);
    the choice has no effect after creation, so the change form omits it
    entirely rather than showing a field that does nothing.
    """

    port_addressing = forms.ChoiceField(
        choices=PortAddressing.choices,
        initial=PortAddressing.STATIC,
        required=False,
        help_text=(
            "Only applies at creation. Ignored (always DHCP) for an unracked device, or for "
            "a port on an L2-only VLAN (no subnet) — neither can carry a static address."
        ),
    )

    class Meta:
        model = NetworkDevice
        exclude: list[str] = []

    def _post_clean(self) -> None:
        # `or`, not `.get(..., default)` alone — required=False means an
        # omitted/blank submission cleans to "" (present in cleaned_data, not
        # absent), and a bare `.get()` default only covers a missing key.
        # `or` covers both that and the ChoiceField-rejected-value case (which
        # does leave the key genuinely absent). Must run before
        # super()._post_clean(), which is what calls self.instance.full_clean().
        self.instance.port_addressing = self.cleaned_data.get("port_addressing") or PortAddressing.STATIC
        super()._post_clean()  # type: ignore[misc]


class RackAddForm(forms.ModelForm):
    """Carries the creation-time-only ``template`` picker (ADR 0014) — not a
    model field (a rack keeps no reference to its template, decision 5), so
    it can't be expressed via ``Meta.fields`` alone. Used only for the add
    view (``RackAdmin.get_form()``); a template has no effect after
    creation, so the change form omits this field entirely.

    Leaving ``slot_count`` blank on this form adopts the chosen template's
    ``slot_count``, if it has one (decision 9) — a server-side fallback on
    submission, not a live-updating prefill: this project has no
    JavaScript, so nothing can populate a value on this page before it's
    submitted.
    """

    template = forms.ModelChoiceField(
        queryset=RackTemplate.objects.all(),
        required=False,
        help_text="Optional. Seeds this rack's VLAN address ranges at creation (ADR 0014). Leave "
        "slot_count blank below to use the template's slot_count, if it has one.",
    )

    class Meta:
        model = Rack
        fields = ["name", "slot_count"]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.fields["slot_count"].required = False

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        template = cleaned_data.get("template")
        if cleaned_data.get("slot_count") is None:
            if template is not None and template.slot_count:
                cleaned_data["slot_count"] = template.slot_count
            else:
                # slot_count is form-required=False only to let the
                # fallback above satisfy it silently — when nothing can
                # (no template, or a template with no slot_count of its
                # own), it must still surface as an ordinary field error
                # here. Without this, Django's ModelForm excludes an
                # empty, form-non-required field from the model's own
                # full_clean() (see BaseModelForm._get_validation_
                # exclusions's "backwards-compatibility" exclusion for
                # exactly this shape), so the blank value would sail
                # through form.is_valid() and only fail later as a raw
                # IntegrityError from the NOT NULL column at save() time.
                self.add_error(
                    "slot_count",
                    "This field is required, unless a template with a slot_count is selected.",
                )
        return cleaned_data

    def _post_clean(self) -> None:
        # template isn't a model field, so construct_instance() (called
        # inside super()._post_clean(), before instance.full_clean()) never
        # touches it — set once here, before super()._post_clean(), so
        # Rack.clean()'s advisory pre-flight (also inside
        # super()._post_clean(), via instance.full_clean()) sees it.
        self.instance.template = self.cleaned_data.get("template")
        super()._post_clean()  # type: ignore[misc]


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
    model = NetworkSwitchTypePort
    formset = NetworkSwitchTypePortFormSet
    fields = ["port_number", "description", "port_type", "profile"]
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).select_related("profile")

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
    fact copied from the type) is locked too; ``description``/``profile``
    stay editable per switch, though ``NetworkSwitchPortForm`` disables
    ``profile`` on a row that already has a connected device port.
    """

    model = NetworkSwitchPort
    form = NetworkSwitchPortForm
    fields = ["port_number", "port_type", "description", "profile", "profile_summary"]
    readonly_fields = ["port_number", "port_type", "profile_summary"]
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # ``profile_summary`` reads mode/native VLAN/allowed VLANs per row, and
        # ``NetworkSwitchPortForm.__init__`` probes ``connected_device_port``
        # per row to decide whether to disable ``profile`` — without all three
        # of these, each is an N+1 across a switch's ports. The reverse
        # one-to-one is ``select_related``-able, and ``hasattr()`` still
        # short-circuits correctly against the cached null.
        return (
            super()
            .get_queryset(request)
            .select_related("profile__native_vlan", "connected_device_port")
            .prefetch_related("profile__allowed_vlans")
        )

    @admin.display(description="Profile config")
    def profile_summary(self, obj: NetworkSwitchPort) -> str:
        profile = obj.profile
        mode = profile.get_port_mode_display()
        if profile.all_vlans_allowed:
            return f"{mode}, all VLANs allowed"
        allowed = ", ".join(str(vlan.vlan_id) for vlan in profile.allowed_vlans.all())
        summary = f"{mode}, native {profile.native_vlan.vlan_id}"
        return f"{summary}, allowed {allowed}" if allowed else summary

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


@admin.register(SwitchPortVlanProfile)
class SwitchPortVlanProfileAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    form = SwitchPortVlanProfileForm
    list_display = [
        "name",
        "port_mode",
        "native_vlan",
        "all_vlans_allowed",
        "allowed_vlans_display",
        "is_system",
    ]
    search_fields = ["name"]
    show_auditlog_history_link = True

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # allowed_vlans_display() reads every row's allowed_vlans — without
        # this, an N+1 across the changelist.
        return super().get_queryset(request).prefetch_related("allowed_vlans")

    @admin.display(description="Allowed VLANs")
    def allowed_vlans_display(self, obj: SwitchPortVlanProfile) -> str:
        return ", ".join(str(vlan_id) for vlan_id in sorted(vlan.vlan_id for vlan in obj.allowed_vlans.all()))

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # Both sets are the same ones SwitchPortVlanProfile.save() itself
        # enforces — no second, hand-maintained list here (ADR 0012).
        if obj is None:
            return []
        if obj.is_system:
            return sorted(_PROFILE_SYSTEM_LOCKED_FIELDS)
        if obj.ports.exists():
            return sorted(_PROFILE_IN_USE_LOCKED_FIELDS)
        return []

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # A profile still in use is already blocked by Django's native
        # protected-object detection (profile FKs are on_delete=PROTECT) —
        # only is_system needs an explicit override, since nothing at the
        # DB level otherwise stops it from being deleted.
        if obj is not None and obj.is_system:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(RackTemplate)
class RackTemplateAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    form = RackTemplateForm
    list_display = ["name", "slot_count", "vlans_display"]
    search_fields = ["name"]
    show_auditlog_history_link = True

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # vlans_display() reads every row's vlans — without this, an N+1
        # across the changelist.
        return super().get_queryset(request).prefetch_related("vlans")

    @admin.display(description="VLANs")
    def vlans_display(self, obj: RackTemplate) -> str:
        return ", ".join(str(vlan_id) for vlan_id in sorted(vlan.vlan_id for vlan in obj.vlans.all()))


@admin.register(Rack)
class RackAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slot_count"]
    search_fields = ["name"]
    inlines = [RackVlanRangeInline]
    show_auditlog_history_link = True

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        # template (ADR 0014) only makes sense at creation — a rack keeps
        # no reference to its template (decision 5), so the change form
        # uses the default ModelForm, which has no such field. There is
        # deliberately no way to filter/search racks by template for the
        # same reason: no rack stores one.
        if obj is None:
            kwargs["form"] = RackAddForm
        return super().get_form(request, obj, change=change, **kwargs)


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

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        # port_addressing (ADR 0013) only makes sense at creation — the
        # change form uses the default ModelForm, which has no such field.
        if obj is None:
            kwargs["form"] = NetworkDeviceAddForm
        return super().get_form(request, obj, change=change, **kwargs)
