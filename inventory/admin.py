from typing import Any

from auditlog.mixins import AuditlogHistoryAdminMixin
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as default_delete_selected
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.forms import BaseInlineFormSet, BaseModelFormSet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path

from .models import (
    _PROFILE_IN_USE_LOCKED_FIELDS,
    _PROFILE_SYSTEM_LOCKED_FIELDS,
    VLAN,
    AuditedModel,
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
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchAddressing,
    SwitchPortVlanProfile,
    _lock_devices_by_pk,
    occupied_rack_slot_ranges,
    switch_port_profile_summary,
)
from .suggestions import lowest_free_run


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

    def delete_view(
        self, request: HttpRequest, object_id: str, extra_context: dict[str, Any] | None = None
    ) -> Any:
        """Same reasoning as ``changeform_view`` above, for the delete
        route: a ``ValidationError`` raised from inside ``Model.delete()``
        (a profile-lock guard, e.g.) would otherwise reach the admin
        unhandled and 500, since Django's delete confirmation view has no
        built-in handling for it (only ``ProtectedError``/
        ``RestrictedError`` get the native "can't delete" page).
        """
        try:
            return super().delete_view(request, object_id, extra_context)  # type: ignore[misc]
        except ValidationError as exc:
            messages.error(request, f"Could not delete: {exc}")
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
            elif isinstance(instance, NetworkDevicePort):
                # ADR 0017 — see
                # NetworkDevicePort.refresh_locked_offset_address()'s
                # docstring for why this must run here (immediately before
                # save, on every existing device-port row) rather than in
                # the model itself.
                instance.refresh_locked_offset_address()
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

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Blocked on the Add page (obj is None) only. ADR 0016's
        # materialization runs from NetworkSwitch.save() — i.e. from
        # save_model(), which the admin calls *before* save_related() saves
        # this inline's formset. Without this, an operator who creates a
        # switch and types an address inline on the same Add page would get
        # both a materialized row and their hand-entered row for the same
        # VLAN — an IntegrityError on unique_switch_vlan_address (a 500,
        # not a form error), and materialization can't see the inline rows
        # to avoid this by inspection because they don't exist yet when it
        # runs. Matches how NetworkDevicePortInline already behaves (`:550`)
        # — MANUAL then means "create the switch, then add its addresses on
        # the change page," a two-step; the change page keeps full add/
        # edit/delete, so nothing becomes impossible.
        if obj is None:
            return False
        return super().has_add_permission(request, obj)


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


def _fill_rack_derived_owner_default(cleaned_data: dict[str, Any], rack: Any) -> None:
    """Fills a blank ``owner`` from ``rack.owner`` (ADR 0023, plan settled
    decision 2) — a creation-time *default*, not inheritance: an explicit
    choice is never overwritten, a rack with no owner leaves the field
    blank, and this only ever runs from an add form's ``clean()`` — the
    change forms and ``objects.create()`` never call it, so neither
    derives. Shared by ``NetworkDeviceAddForm``/``NetworkSwitchAddForm``,
    the same shape as the ``rack_slot`` lowest-free-ordinal fill (ADR 0019)
    a few lines away in each.
    """
    if rack is not None and not cleaned_data.get("owner"):
        cleaned_data["owner"] = rack.owner


class NetworkDeviceAddForm(forms.ModelForm):
    """Carries the creation-time-only ``port_addressing`` choice (ADR 0013)
    — not a model field, so it can't be expressed via ``Meta.fields``
    alone. Used only for the add view (``NetworkDeviceAdmin.get_form()``);
    the choice has no effect after creation, so the change form omits it
    entirely rather than showing a field that does nothing.

    Also carries one ``GenericIPAddressField`` per ``OPERATOR``-sourced
    Network Device Type Port on the chosen type (ADR 0022) — a Yamaha
    console's Device Control interface, e.g. — labelled from the port's
    ``description``. These aren't model fields either; ``clean()``
    assembles them into ``self.instance.operator_addresses``, the
    transient property ``_materialize_ports()`` reads from.

    Which fields exist depends on ``device_type``, which isn't known until
    a device type is actually chosen — this class alone only ever adds
    them from ``__init__`` (self.data on a submission, self.initial on a
    prefilled GET, e.g. the spare-pool deep link), onto the *instance*.
    That's enough for direct construction (as every test in this codebase
    that builds this form does), but **not** enough for the real admin
    view (Codex review of PR 1, P1): Django's ``ModelAdmin`` computes the
    fieldset it renders from the form *class*'s ``base_fields`` — set once
    when ``modelform_factory()`` builds the class, before any instance's
    ``__init__`` ever runs — so a field added only in ``__init__`` is
    genuinely present in ``self.fields`` (form validation sees it fine)
    but never appears in the rendered page at all. ``NetworkDeviceAdmin.
    get_form()`` is what actually fixes this: it builds a fresh subclass
    with these fields *declared* (so they land in ``base_fields``) for
    each request, via :meth:`with_operator_fields`, and passes that
    subclass in as the form to use instead of this bare class. This class
    keeps its own ``__init__``-based fallback too, since it costs nothing
    and keeps every direct-construction test (and any future non-admin
    caller) working unchanged.

    Required is deliberately ``False`` (Codex review of PR 1, P2) — an
    unracked device, an explicit DHCP choice, or an operator port on an
    L2-only VLAN all materialize DHCP and ignore ``operator_addresses``
    entirely (``NetworkDevice._materialize_ports()``), so a blank field in
    those cases means nothing and must not block submission.
    ``clean()``'s ``_validate_operator_addresses()`` adds the field error
    back in exactly the cases where the device *will* actually
    materialize the port statically.
    """

    #: Prefix for the per-type-port dynamic fields above — never collides
    #: with a real model field name, so ``cleaned_data`` keys built from it
    #: can be told apart from everything else Meta.fields draws in.
    _OPERATOR_ADDRESS_FIELD_PREFIX = "operator_address__"

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
        # owner/hostname_purpose/hostname_sequence (ADR 0023, plan settled
        # decision 2) must be listed here explicitly — this is an explicit
        # list, not exclude=[], so construct_instance() silently drops any
        # field left out of it, including the rack-derived owner default
        # clean() below computes.
        fields = [
            "device_type",
            "hostname",
            "serial_number",
            "rack",
            "rack_slot",
            "owner",
            "hostname_purpose",
            "hostname_sequence",
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # One field per OPERATOR-sourced type port on the chosen type (ADR
        # 0022) — self.data (bound: a submission) takes priority over
        # self.initial (unbound: a prefilled GET), matching how every
        # other ModelChoiceField on this form resolves its value. Adds to
        # ``self.fields`` (the instance), which is enough for direct
        # construction; see the class docstring for why the real admin
        # view needs ``with_operator_fields()`` as well.
        device_type_id = (self.data or {}).get("device_type") or self.initial.get("device_type")
        self._operator_type_ports = self._operator_type_ports_for(device_type_id)
        for type_port in self._operator_type_ports:
            field_name = self._operator_address_field_name(type_port)
            if field_name not in self.fields:
                self.fields[field_name] = self._operator_address_field(type_port)

    @staticmethod
    def _operator_type_ports_for(device_type_id: Any) -> list[NetworkDeviceTypePort]:
        """Every ``OPERATOR``-sourced Network Device Type Port on
        ``device_type_id``, or ``[]`` if none is given — shared by
        ``__init__`` (instance fields, for direct construction) and
        ``with_operator_fields()`` (class-level fields, for the real admin
        view).

        Coerces to ``int`` here, defensively, rather than trusting a caller
        to have already validated it (Codex review of ADR 0022 PR 3, P2) —
        ``__init__`` reads this straight from ``self.data``/``self.initial``
        (an unvalidated request value) on *every* construction, not only
        through ``with_operator_fields()``, so a fix only at that one call
        site (ADR 0022 PR 3's fit view) would still have left this the raw,
        crashing path for a crafted ``device_type`` reaching ``__init__``
        directly — a bare ``QuerySet.filter(device_type_id=<garbage>)``
        raises ``ValueError`` before ``ModelChoiceField`` ever gets a
        chance to turn it into an ordinary form error. A non-coercible value
        degrades to "no type chosen yet" (``[]``), the same shape a blank
        value already produces.
        """
        if not device_type_id:
            return []
        try:
            device_type_id = int(device_type_id)
        except (TypeError, ValueError):
            return []
        return list(
            NetworkDeviceTypePort.objects.filter(
                device_type_id=device_type_id, address_source=PortAddressSource.OPERATOR
            )
            .select_related("vlan")
            .order_by("ordinal")
        )

    @classmethod
    def _operator_address_field_name(cls, type_port: NetworkDeviceTypePort) -> str:
        return f"{cls._OPERATOR_ADDRESS_FIELD_PREFIX}{type_port.pk}"

    @staticmethod
    def _operator_address_field(type_port: NetworkDeviceTypePort) -> forms.GenericIPAddressField:
        # required=False — see the class docstring (Codex review of PR 1,
        # P2). clean()'s _validate_operator_addresses() adds the field
        # error back in exactly the cases where this device will actually
        # materialize the port statically.
        return forms.GenericIPAddressField(
            protocol="IPv4",
            required=False,
            label=type_port.description,
            help_text=(
                f"Static address for {type_port.description} on {type_port.vlan} — the system "
                "has no way to compute this one (ADR 0022). Required only if this device will "
                "get a static address; ignored (and may be left blank) for an unracked device "
                "or an explicit DHCP choice."
            ),
        )

    @classmethod
    def with_operator_fields(cls, device_type_id: Any) -> type["NetworkDeviceAddForm"]:
        """A subclass of this form with one ``GenericIPAddressField`` per
        ``OPERATOR``-sourced type port on ``device_type_id`` *declared at
        the class level* — not just added to an instance's ``self.fields``
        the way ``__init__`` does above.

        This is what makes the fields actually render (Codex review of PR
        1, P1): ``NetworkDeviceAdmin.get_form()`` calls this to build the
        form class it hands to Django's admin machinery, which computes
        the rendered fieldset from ``form.base_fields`` — a class-level
        attribute the ``ModelFormMetaclass`` populates once, from the
        class body, when the class is created. A field ``__init__`` adds
        later is real (validation sees it) but invisible (nothing in the
        fieldset names it), so a type with an ``OPERATOR`` port could
        never actually be created through the admin before this existed:
        the field's own ``required=False`` (see above) would have let the
        submission past *were* it rendered, but it never was, so the
        browser never sent a value, and there was nothing on the page
        allowing an operator to supply one either.

        Returns this class unchanged when ``device_type_id`` has no
        ``OPERATOR`` ports (or is unset) — the overwhelmingly common case,
        which shouldn't pay for a needless dynamic subclass.
        """
        type_ports = cls._operator_type_ports_for(device_type_id)
        if not type_ports:
            return cls
        extra_fields = {
            cls._operator_address_field_name(type_port): cls._operator_address_field(type_port)
            for type_port in type_ports
        }
        return type(cls.__name__, (cls,), extra_fields)

    def clean(self) -> dict[str, Any]:
        """A blank ``rack_slot`` is filled in with the lowest free ordinal
        (ADR 0019) — never overwriting a typed value.

        Bails outright unless both ``rack`` and ``device_type`` cleaned: a
        field error on either means the span needed for the search is
        unknowable, so the ordinary field error is left to surface on its
        own.
        """
        cleaned_data = super().clean() or {}
        rack = cleaned_data.get("rack")
        _fill_rack_derived_owner_default(cleaned_data, rack)
        device_type = cleaned_data.get("device_type")
        if rack is None or device_type is None:
            return cleaned_data
        host_slot = cleaned_data.get("rack_slot")
        if host_slot is None:
            occupied = occupied_rack_slot_ranges(rack)
            host_slot = lowest_free_run(occupied, device_type.slot_span, rack.slot_count)
            if host_slot is None:
                self.add_error(
                    "rack_slot",
                    f"No free rack slot for a span of {device_type.slot_span} in {rack} "
                    f"(slot_count {rack.slot_count}).",
                )
                return cleaned_data
            cleaned_data["rack_slot"] = host_slot
        self._validate_operator_addresses(cleaned_data)
        return cleaned_data

    def _validate_operator_addresses(self, cleaned_data: dict[str, Any]) -> None:
        """Requires an address for each ``OPERATOR`` type port only when
        this device will actually materialize it statically (ADR 0022;
        Codex review of PR 1, P2) — ``NetworkDevice._materialize_ports()``
        ignores ``operator_addresses`` entirely for an unracked device, an
        explicit DHCP choice, or a port on an L2-only VLAN, and the field
        itself is ``required=False`` for exactly that reason (see the
        class docstring). Called only from the tail of ``clean()``, where
        ``rack``/``device_type`` both cleaned and a ``rack_slot`` was
        found — every earlier bail-out in ``clean()`` is itself a case
        where this device won't productively materialize statically, so
        skipping the check there is correct, not merely convenient.
        """
        port_addressing = cleaned_data.get("port_addressing") or PortAddressing.STATIC
        if port_addressing != PortAddressing.STATIC:
            return
        for type_port in self._operator_type_ports:
            if not type_port.vlan.subnet:
                continue  # L2-only VLAN — always materializes DHCP regardless of port_addressing
            field_name = self._operator_address_field_name(type_port)
            if not cleaned_data.get(field_name):
                self.add_error(
                    field_name, "This field is required for a device that will get a static address."
                )

    def _post_clean(self) -> None:
        # `or`, not `.get(..., default)` alone — required=False means an
        # omitted/blank submission cleans to "" (present in cleaned_data, not
        # absent), and a bare `.get()` default only covers a missing key.
        # `or` covers both that and the ChoiceField-rejected-value case (which
        # does leave the key genuinely absent). Must run before
        # super()._post_clean(), which is what calls self.instance.full_clean().
        self.instance.port_addressing = self.cleaned_data.get("port_addressing") or PortAddressing.STATIC
        # Assembled from the dynamic per-type-port fields __init__() added
        # (ADR 0022), keyed by description — exactly what
        # NetworkDevice._materialize_ports() reads from. A field missing
        # from cleaned_data (this row's own validation failed) is simply
        # omitted here; the model's own pre-flight reports the missing
        # address by name rather than this silently supplying a blank one.
        self.instance.operator_addresses = {
            type_port.description: self.cleaned_data[f"{self._OPERATOR_ADDRESS_FIELD_PREFIX}{type_port.pk}"]
            for type_port in self._operator_type_ports
            if f"{self._OPERATOR_ADDRESS_FIELD_PREFIX}{type_port.pk}" in self.cleaned_data
        }
        super()._post_clean()  # type: ignore[misc]


class NetworkDeviceChangeForm(forms.ModelForm):
    """Plain change form for an existing device — deliberately distinct
    from ``NetworkDeviceAddForm`` (same shape as ``NetworkSwitchAddForm``/
    the default ``ModelForm`` split elsewhere here): ``port_addressing``
    and the ``OPERATOR``-port address fields only make sense at creation,
    so the change form omits them entirely rather than showing fields that
    do nothing.
    """

    class Meta:
        model = NetworkDevice
        fields = [
            "device_type",
            "hostname",
            "serial_number",
            "rack",
            "rack_slot",
            "owner",
            "hostname_purpose",
            "hostname_sequence",
        ]


class NetworkSwitchAddForm(forms.ModelForm):
    """Carries the creation-time-only ``address_materialization`` choice
    (ADR 0016) — not a model field, so it can't be expressed via
    ``Meta.fields`` alone. Used only for the add view
    (``NetworkSwitchAdmin.get_form()``); the choice has no effect after
    creation, so the change form omits it entirely rather than showing a
    field that does nothing. See ``NetworkDeviceAddForm`` — same shape.

    A blank ``rack_slot`` with a chosen ``rack`` is filled in with the
    lowest free ordinal (ADR 0019) — a switch always spans 1. This is
    add-only, same reasoning as ``NetworkDeviceAddForm``'s: on the change
    form, blank has no such meaning.
    """

    address_materialization = forms.ChoiceField(
        choices=SwitchAddressing.choices,
        initial=SwitchAddressing.STATIC,
        required=False,
        help_text=(
            "Only applies at creation. Ignored (no addresses materialize) for an unracked "
            "switch. MANUAL means add this switch's addresses yourself on the change page."
        ),
    )

    class Meta:
        model = NetworkSwitch
        exclude: list[str] = []

    def clean(self) -> dict[str, Any]:
        cleaned_data = super().clean() or {}
        rack = cleaned_data.get("rack")
        _fill_rack_derived_owner_default(cleaned_data, rack)
        if rack is not None and cleaned_data.get("rack_slot") is None:
            slot = lowest_free_run(occupied_rack_slot_ranges(rack), 1, rack.slot_count)
            if slot is None:
                self.add_error(
                    "rack_slot",
                    f"No free rack slot for a span of 1 in {rack} (slot_count {rack.slot_count}).",
                )
            else:
                cleaned_data["rack_slot"] = slot
        return cleaned_data

    def _post_clean(self) -> None:
        # `or`, not `.get(..., default)` alone — see NetworkDeviceAddForm's
        # identical comment: required=False means an omitted/blank
        # submission cleans to "" (present in cleaned_data, not absent),
        # and a bare `.get()` default only covers a missing key. Must run
        # before super()._post_clean(), which is what calls
        # self.instance.full_clean().
        self.instance.address_materialization = (
            self.cleaned_data.get("address_materialization") or SwitchAddressing.STATIC
        )
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


def _clean_device_type_id(raw: str | None) -> int | None:
    """Best-effort int coercion for a raw, unvalidated ``device_type`` POST
    value — used only to decide which ``OPERATOR``-port fields ``With_
    operator_fields()`` should declare on the create-a-card form, never to
    look a row up directly.

    A malformed value (``"not-an-int"``, from a crafted POST) must fall
    through to the ordinary, already-robust ``ModelChoiceField`` validation
    on the resulting form — which converts exactly this shape of bad input
    into a field error — rather than reach a raw ``QuerySet.filter(device_
    type_id=raw)`` call, which raises a bare ``ValueError`` past any form
    (Codex review, P2; ``NetworkDeviceAdmin._fit_new_card``). Returning
    ``None`` here reproduces the "no type chosen yet" shape ``with_operator_
    fields()``/``_operator_type_ports_for()`` already handle.
    """
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


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
    fields = [
        "port_number",
        "description",
        "port_type",
        "vlan",
        "slot_offset",
        "address_source",
        "hostname_suffix",
    ]

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "devices"):
            return False
        return super().has_add_permission(request, obj)

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Deliberately *not* gated on _profile_locked() (ADR 0022 review
        # note 9) — a locked profile still needs to let a change POST
        # through, because hostname_suffix is exempt from the profile lock
        # at the model layer (NetworkDeviceTypePort._hostname_suffix_only_
        # edit()). Returning False outright here, as this used to, would
        # make that model-level exemption unreachable through the admin.
        # get_readonly_fields() below is what actually locks every other
        # field once the profile has instances.
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        if _profile_locked(obj, "devices"):
            return False
        return super().has_delete_permission(request, obj)

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # hostname_suffix stays editable even once the profile is locked
        # (ADR 0022 decision 4) — every other field freezes, matching
        # NetworkDeviceTypePort's own model-layer exemption.
        if _profile_locked(obj, "devices"):
            return ["port_number", "description", "port_type", "vlan", "slot_offset", "address_source"]
        return []


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
        # Shared with the read-only UI's generic parity page (phase 15
        # Stage B) — see switch_port_profile_summary()'s docstring.
        return switch_port_profile_summary(obj)

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


class NetworkDevicePortForm(forms.ModelForm):
    """Disables ``address`` on a materialized offset port (``slot_offset``
    > 0, ADR 0017) — its address is derived from the offset-0 port on the
    same VLAN and locked at the model level
    (``NetworkDevicePort._locked_fields()``); this is the admin-form half
    of that same lock, same ``disabled=True`` reasoning as
    ``NetworkSwitchPortForm`` (``InlineModelAdmin.get_readonly_fields()``
    can't vary per row, and ``disabled=True`` — not just omitting the field
    — stops a crafted POST from smuggling a value past it, since Django
    ignores a disabled field's submitted data and keeps the form's initial
    value instead).
    """

    class Meta:
        model = NetworkDevicePort
        fields = [
            "description",
            "port_number",
            "port_type",
            "vlan",
            "slot_offset",
            "is_dhcp",
            "address",
            "switch_port",
        ]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.slot_offset > 0:
            self.fields["address"].disabled = True


class NetworkDevicePortInline(admin.TabularInline):
    """See ``NetworkSwitchPortInline`` — same materialized-only reasoning.
    ``description``/``vlan``/``port_type``/``slot_offset`` are locked; only
    DHCP/address/the connected switch port stay editable — and, on a
    ``slot_offset > 0`` row, ``address`` is disabled too
    (``NetworkDevicePortForm``, ADR 0017): that port's address is derived
    from the offset-0 port on the same VLAN, not independently settable.
    ``default_gateway`` is a read-only derived property (ADR 0010), shown
    but never editable.
    """

    model = NetworkDevicePort
    form = NetworkDevicePortForm
    fields = [
        "description",
        "port_number",
        "port_type",
        "vlan",
        "slot_offset",
        "is_dhcp",
        "address",
        "default_gateway",
        "switch_port",
    ]
    readonly_fields = ["description", "port_number", "vlan", "port_type", "slot_offset", "default_gateway"]
    extra = 0

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # ``vlan`` is readonly-displayed and ``default_gateway`` reads it
        # live per row — without this, both N+1 across a device's ports.
        return super().get_queryset(request).select_related("vlan")

    def has_add_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(Department)
class DepartmentAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "description"]
    search_fields = ["name"]
    show_auditlog_history_link = True


@admin.register(Owner)
class OwnerAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slug"]
    search_fields = ["name", "slug"]
    show_auditlog_history_link = True


@admin.register(VLAN)
class VLANAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = [
        "department",
        "name",
        "vlan_id",
        "subnet",
        "default_gateway",
        "dhcp_range_start",
        "dhcp_range_end",
    ]
    search_fields = ["name", "vlan_id", "subnet"]
    ordering = ["vlan_id"]
    list_filter = ["department"]
    # A declarative attribute, not a get_queryset() override — and it has
    # to be, for a reason Django's own auto-select_related() doesn't cover.
    # ChangeList.has_related_field_in_list_display() does auto-apply
    # select_related() whenever a real FK appears in list_display, which
    # would make this redundant *if* department were required — but
    # select_related_descend() (Django 6.0.7) reads `if not restricted:
    # return not field.null`, so the bare auto-applied call descends
    # nothing for a nullable FK. department is nullable (ADR 0021 decision
    # 2), so the N+1 across the changelist is real, and naming the field
    # here is what takes the `restricted` branch and actually fixes it.
    list_select_related = ["department"]
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
    list_display = ["name", "slot_count", "owner", "location_slug"]
    search_fields = ["name", "location_slug"]
    list_filter = ["owner"]
    # A declarative attribute, not relying on Django's auto-select_related()
    # — owner is nullable, so the auto-apply path doesn't descend it (see
    # VLANAdmin's identical comment above for the mechanism).
    list_select_related = ["owner"]
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
    list_display = ["manufacturer", "model", "name", "port_count", "hostname_slug"]
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
    list_display = [
        "hostname",
        "switch_type",
        "serial_number",
        "rack",
        "rack_slot",
        "dhcp_server_enabled",
        "owner",
        "hostname_purpose",
        "hostname_sequence",
    ]
    search_fields = ["hostname", "serial_number"]
    list_filter = ["rack", "switch_type", "owner"]
    # Declarative, not relied-on auto-select_related() — owner/rack/switch_type
    # are all nullable-or-not-descended the same way VLANAdmin's comment
    # above explains.
    list_select_related = ["switch_type", "rack", "owner"]
    inlines = [NetworkSwitchAddressInline, NetworkSwitchPortInline]
    show_auditlog_history_link = True
    actions = [delete_selected]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # switch_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is not None:
            return ["switch_type"]
        return []

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        # address_materialization (ADR 0016) only makes sense at creation —
        # the change form uses the default ModelForm, which has no such
        # field.
        if obj is None:
            kwargs["form"] = NetworkSwitchAddForm
        return super().get_form(request, obj, change=change, **kwargs)

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
    list_display = ["manufacturer", "model", "name", "port_count", "is_add_in_card", "hostname_slug"]
    list_filter = ["is_add_in_card"]
    search_fields = ["manufacturer", "model", "name"]
    inlines = [NetworkDeviceTypePortInline]
    show_auditlog_history_link = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # ADR 0010's lock on manufacturer/model/name/port_count once the
        # profile has instances: NetworkDeviceType.save()/clean() lock it
        # identically. is_add_in_card joins the lock (ADR 0022 PR 3) —
        # flipping it after instances exist would either strand fitted
        # devices or retroactively offer ordinary equipment to the fit
        # picker.
        if _profile_locked(obj, "devices"):
            return ["manufacturer", "model", "name", "port_count", "is_add_in_card"]
        return []


@admin.register(NetworkDevice)
class NetworkDeviceAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = [
        "hostname",
        "device_type",
        "serial_number",
        "rack",
        "rack_slot",
        "host",
        "owner",
        "hostname_purpose",
        "hostname_sequence",
    ]
    search_fields = ["hostname", "serial_number"]
    # ("host", EmptyFieldListFilter) gives fitted/unfitted as the filter
    # choices (ADR 0022 PR 3) — a plain "host" filter would instead list
    # every individual host, which isn't the question this filter answers.
    list_filter = ["rack", "device_type", ("host", admin.EmptyFieldListFilter), "owner"]
    list_select_related = ["device_type", "rack", "host", "owner"]
    inlines = [NetworkDevicePortInline]
    show_auditlog_history_link = True
    actions = ["pull_cards"]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # device_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is None:
            return []
        return ["device_type"]

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        # port_addressing/operator-address inputs (ADR 0013/0022) only
        # make sense at creation. Two distinct forms, same shape as
        # NetworkSwitchAddForm/the default ModelForm split elsewhere here.
        if obj is None:
            # NetworkDeviceAddForm.with_operator_fields() (ADR 0022; Codex
            # review of PR 1, P1) — a form class built per request, with
            # this request's OPERATOR-port fields *declared* rather than
            # merely instance-added, so Django's admin machinery (which
            # computes the rendered fieldset from the form class's
            # base_fields, before any instance's __init__ ever runs) can
            # actually see and render them. request.POST (a submission)
            # takes priority over request.GET (a prefilled deep link),
            # matching NetworkDeviceAddForm.__init__'s own self.data-over-
            # self.initial resolution for the same field.
            device_type_id = request.POST.get("device_type") or request.GET.get("device_type")
            kwargs["form"] = NetworkDeviceAddForm.with_operator_fields(device_type_id)
        else:
            kwargs["form"] = NetworkDeviceChangeForm
        return super().get_form(request, obj, change=change, **kwargs)

    # -- Pull (ADR 0022 PR 3) -----------------------------------------------------

    @admin.action(permissions=["change"], description="Pull selected cards from their host")
    def pull_cards(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Clears ``host`` on every selected, currently-fitted card through
        an ordinary, audited ``save()`` per row — never ``queryset.
        update()``, which would leave no trace on the card's own history
        (ADR 0022 PR 3's Audit section). Leaves ``rack``, ``rack_slot`` and
        every address alone: a pulled card keeps its address in the pool.

        ``permissions=["change"]`` restricts this action to users who hold
        ``inventory.change_networkdevice`` — Django's own
        ``_filter_actions_by_permissions`` mechanism, not a hand-rolled
        check.
        """
        pulled = 0
        skipped = 0
        for card in queryset:
            with transaction.atomic():
                locked = _lock_devices_by_pk(card.pk)
                current = locked.get(card.pk)
                if current is None or current.host_id is None:
                    skipped += 1
                    continue
                current.host = None
                current.save()
                pulled += 1
        if pulled:
            messages.success(request, f"Pulled {pulled} card(s) from their host.")
        if skipped:
            messages.info(request, f"{skipped} selected device(s) had no host to pull.")

    # -- Fit a card (ADR 0022 PR 3) -----------------------------------------------

    def get_urls(self) -> list[URLPattern]:
        custom = [
            path(
                "<int:host_id>/fit-card/",
                self.admin_site.admin_view(self.fit_card_view),
                name="inventory_networkdevice_fit_card",
            ),
        ]
        return custom + super().get_urls()

    def fit_card_view(self, request: HttpRequest, host_id: int) -> HttpResponse:
        """The dedicated "fit a card" flow (ADR 0022 PR 3), reached from an
        object tool on a host's change page (``admin/inventory/
        networkdevice/change_form_object_tools.html``). Two mutually
        exclusive paths: choosing an existing hostless card, or creating a
        new one. GET only renders the picker; every mutation is POST, and
        wrapped (via ``get_urls()``) in ``admin_site.admin_view`` for the
        same login/staff gating every other admin view gets.

        Host and card/type are re-validated server-side inside a locked
        transaction regardless of what either picker's own queryset would
        have offered — a crafted POST naming a non-card type or a
        card-typed host is refused by ``NetworkDevice._check_host_
        invariants()`` (via ``full_clean()``/``save()``), not merely hidden
        from view by the queryset restriction.

        ``admin_site.admin_view`` (``get_urls()``) supplies only login/staff
        gating, the same as every other admin view — it never checks a
        model permission (Codex review, P2). Without an explicit check here,
        a staff user holding *no* ``NetworkDevice`` permission at all could
        GET this URL directly and see the host and every hostless card
        rendered before either per-section ``can_use_existing``/``can_
        create`` guard in the template ever runs. The floor is "holds at
        least one of the two capabilities this page offers" — the same
        change-or-add posture ``_fit_existing_card``/``_fit_new_card``
        individually require, checked before any row is fetched.
        """
        if not (
            request.user.has_perm("inventory.change_networkdevice")
            or request.user.has_perm("inventory.add_networkdevice")
        ):
            raise PermissionDenied
        host = get_object_or_404(NetworkDevice.objects.select_related("device_type"), pk=host_id)
        if host.device_type.is_add_in_card:
            messages.error(request, f"{host} is itself an add-in card and cannot host another.")
            return redirect("admin:inventory_networkdevice_change", host.pk)

        if request.method == "POST":
            mode = request.POST.get("fit_mode")
            if mode == "existing":
                return self._fit_existing_card(request, host)
            if mode == "create":
                return self._fit_new_card(request, host)
            messages.error(request, "Unrecognized fit action.")
            return redirect(request.path)

        return self._render_fit_card_page(request, host)

    def _render_fit_card_page(
        self, request: HttpRequest, host: NetworkDevice, create_form: NetworkDeviceAddForm | None = None
    ) -> TemplateResponse:
        if create_form is None:
            create_form = NetworkDeviceAddForm.with_operator_fields(None)(instance=NetworkDevice(host=host))
        create_form.fields["device_type"].queryset = NetworkDeviceType.objects.filter(  # type: ignore[attr-defined]
            is_add_in_card=True
        )
        existing_cards = (
            NetworkDevice.objects.filter(device_type__is_add_in_card=True, host__isnull=True)
            .exclude(pk=host.pk)
            .select_related("device_type")
            .order_by("hostname")
        )
        context = {
            **self.admin_site.each_context(request),
            "title": f"Fit a card to {host}",
            "opts": self.model._meta,
            "original": host,
            "host": host,
            "existing_cards": existing_cards,
            "create_form": create_form,
            # The existing-card path needs change on both rows (this app has
            # no per-object permissions — see CONTEXT.md's roles — so
            # "both rows" reduces to holding the one change_networkdevice
            # codename); the create path needs add plus change, since the
            # card row doesn't exist yet (review note 4).
            "can_use_existing": request.user.has_perm("inventory.change_networkdevice"),
            "can_create": (
                request.user.has_perm("inventory.add_networkdevice")
                and request.user.has_perm("inventory.change_networkdevice")
            ),
        }
        return TemplateResponse(request, "admin/inventory/networkdevice/fit_card.html", context)

    def _fit_existing_card(self, request: HttpRequest, host: NetworkDevice) -> HttpResponse:
        if not request.user.has_perm("inventory.change_networkdevice"):
            raise PermissionDenied
        try:
            card_id = int(request.POST.get("card", ""))
        except (TypeError, ValueError):
            messages.error(request, "Choose a card to fit.")
            return redirect(request.path)
        with transaction.atomic():
            locked = _lock_devices_by_pk(host.pk, card_id)
            locked_host = locked.get(host.pk)
            card = locked.get(card_id)
            if locked_host is None:
                messages.error(request, "That host no longer exists.")
                return redirect("admin:inventory_networkdevice_changelist")
            if card is None:
                messages.error(request, "That card no longer exists.")
                return redirect(request.path)
            if card.pk == locked_host.pk:
                messages.error(request, "A device cannot be fitted to itself.")
                return redirect(request.path)
            if locked_host.device_type.is_add_in_card:
                messages.error(request, f"{locked_host} is itself an add-in card and cannot host another.")
                return redirect("admin:inventory_networkdevice_change", locked_host.pk)
            if not card.device_type.is_add_in_card:
                messages.error(request, f"{card} is not an add-in card type.")
                return redirect(request.path)
            if card.host_id is not None:
                messages.error(request, f"{card} is already fitted to a host.")
                return redirect(request.path)
            card.host = locked_host
            try:
                card.full_clean()
                card.save()
            except ValidationError as exc:
                messages.error(request, f"Could not fit card: {exc}")
                return redirect(request.path)
        messages.success(request, f"{card} fitted to {locked_host}.")
        return redirect("admin:inventory_networkdevice_change", locked_host.pk)

    def _fit_new_card(self, request: HttpRequest, host: NetworkDevice) -> HttpResponse:
        if not (
            request.user.has_perm("inventory.add_networkdevice")
            and request.user.has_perm("inventory.change_networkdevice")
        ):
            raise PermissionDenied
        device_type_id = _clean_device_type_id(request.POST.get("device_type"))
        form_cls = NetworkDeviceAddForm.with_operator_fields(device_type_id)
        with transaction.atomic():
            locked = _lock_devices_by_pk(host.pk)
            locked_host = locked.get(host.pk)
            if locked_host is None:
                messages.error(request, "That host no longer exists.")
                return redirect("admin:inventory_networkdevice_changelist")
            if locked_host.device_type.is_add_in_card:
                messages.error(request, f"{locked_host} is itself an add-in card and cannot host another.")
                return redirect("admin:inventory_networkdevice_change", locked_host.pk)
            # NetworkDeviceAddForm.with_operator_fields() (review note 5) —
            # a bespoke form here would silently bypass that form's rack-
            # slot suggester, operator-address fields and materialization
            # pre-flight. instance=NetworkDevice(host=locked_host) so
            # full_clean() sees the relationship and the row is inserted
            # once with host already set, never patched in afterward.
            form = form_cls(data=request.POST, instance=NetworkDevice(host=locked_host))
            form.fields["device_type"].queryset = NetworkDeviceType.objects.filter(  # type: ignore[attr-defined]
                is_add_in_card=True
            )
            if not form.is_valid():
                return self._render_fit_card_page(request, locked_host, create_form=form)
            form.instance.created_by = request.user
            new_card = form.save()
        messages.success(request, f"{new_card} created and fitted to {host}.")
        return redirect("admin:inventory_networkdevice_change", host.pk)
