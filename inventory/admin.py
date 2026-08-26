import functools
from typing import Any, NamedTuple

from auditlog.mixins import AuditlogHistoryAdminMixin
from django import forms
from django.contrib import admin, messages
from django.contrib.admin.actions import delete_selected as default_delete_selected
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Max, QuerySet
from django.forms import BaseInlineFormSet, BaseModelFormSet
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.urls import URLPattern, path

from . import dante
from .dante import UnitIdSuggestion
from .hostnames import HostnameComponents, assemble_hostname, choose_sequence, resolve_explicit_sequence
from .models import (
    _PROFILE_IN_USE_LOCKED_FIELDS,
    _PROFILE_SYSTEM_LOCKED_FIELDS,
    VLAN,
    AuditedModel,
    Department,
    NetworkDevice,
    NetworkDeviceModel,
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
    PortMode,
    Rack,
    RackTemplate,
    RackVlanRange,
    SwitchAddressing,
    SwitchPortVlanProfile,
    _candidate_range_is_free,
    _format_allocation,
    _lock_devices_by_pk,
    _vlan_alignment_input,
    occupied_rack_slot_ordinals,
    occupied_rack_slot_ranges,
    switch_port_profile_summary,
)
from .suggestions import (
    lowest_free_placement,
    lowest_free_run,
    range_at_offset,
    range_offset,
    suggest_aligned_offset,
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
    """Implements ADR 0014 decision 11 (the template/manual-VLAN conflict
    check) and ADR 0025 decision 7 (one joint offset across everything
    this submission is about to allocate).

    Reads ``self.instance.template`` — populated by ``RackAddForm.
    _post_clean()`` before this formset's own ``clean()`` runs. This works
    because ``formset.instance`` *is* ``form.instance`` (the same object,
    not a copy) and Django's admin ``_changeform_view`` only calls
    ``all_valid(formsets)`` (which is what triggers formset validation)
    *after* ``form.is_valid()`` has already run ``_post_clean()`` — even
    though the formsets themselves are *constructed* earlier, before
    ``form.is_valid()`` runs. If a future Django release reorders that,
    the template-conflict check below degrades to the raw IntegrityError
    decision 11 forbids rather than silently doing nothing —
    ``RackTemplateAdminTests`` asserts the *form error* specifically so
    that regression fails loudly. On the rack change form (no
    ``template`` field at all), ``self.instance.template`` is simply the
    property's default ``None`` and that check is a no-op, exactly as
    intended.
    """

    def clean(self) -> None:
        super().clean()
        if any(self.errors):
            return
        template = getattr(self.instance, "template", None)
        template_vlan_ids: set[int] = set()
        if template is not None:
            template_vlan_ids = set(template.vlan_links.values_list("vlan_id", flat=True))
        if template_vlan_ids:
            for form in self.forms:
                if not form.cleaned_data or form.cleaned_data.get("DELETE", False):
                    continue
                vlan = form.cleaned_data.get("vlan")
                if vlan is not None and vlan.pk in template_vlan_ids:
                    raise forms.ValidationError(
                        f"{vlan} is already included by the selected Rack Template — remove this "
                        "row or choose a different VLAN; it will be allocated by the template "
                        "automatically."
                    )
        self._align_offsets(template, template_vlan_ids)

    def _align_offsets(self, template: "RackTemplate | None", template_vlan_ids: "set[int]") -> None:
        """ADR 0025 decision 7: one joint offset over the template's VLANs
        (which have no ``RackVlanRange`` rows yet — ``_apply_template()``
        only creates them once ``Rack.save()`` runs, *after* this formset
        validates) and this formset's own **user-blank** rows, together.

        A blank row's ``form.instance.address_range`` already holds a
        per-VLAN first-fit suggestion by this point — ``instance.
        full_clean()`` (called from ``form.is_valid()``, which runs before
        ``formset.clean()``) already filled it in. So "blank" is read from
        ``form.cleaned_data["address_range"]`` (the *submitted* value,
        still ``""``), not the instance — the plan's "ordering trap".

        "Anchors" are ranges already fixed by this submission: a
        manually-typed non-blank value here, or (the change-page case) an
        unchanged existing row — both surface identically through
        ``form.cleaned_data``, since Django prefills a bound form's
        initial value as its "submitted" one when the operator leaves it
        untouched — plus, for completeness, any already-saved range this
        formset has no form for at all (review note 4). Deleted rows are
        excluded from all of it, matching the DELETE skip above.

        If the anchors agree on one offset and it's free on every VLAN
        being aligned, that offset wins. With no anchors at all, a fresh
        ``suggest_aligned_offset()`` search runs over exactly those VLANs
        (decision 6 — never the whole VLAN set). Either way, a winning
        offset is stashed on ``self.instance._aligned_offset`` so
        ``Rack._apply_template()`` — which independently needs the same
        offset for the template's own rows — doesn't have to (and can't)
        rediscover it after the fact; see that method's docstring for the
        other half of this trick. Anything short of a winning offset
        leaves every blank row's already-computed first-fit value alone
        and records an advisory naming each blank row's own outcome
        (decision 3 — fall back, and say so; the template's own rows get
        their own advisory from ``_apply_template()`` itself, once it
        decides their fallback addresses, since this method runs before
        those rows even exist).

        ``self.instance._aligned_offset_attempted`` is set unconditionally
        below, before any of this method's own early returns — it tells
        ``_apply_template()`` whether a joint search over the *union*
        already ran and, if so, forbids it from quietly narrowing to a
        fresh search over just the template's own VLANs when that union
        search failed (Codex review finding 2). Without it, a failed
        three-way search (template VLANs A/B plus inline VLAN C, say)
        would let A and B end up aligned with each other while C alone
        goes independent — exactly the "a subset gets quietly aligned"
        outcome decisions 2 and 3 forbid; the fix is that when nothing
        common exists across everything being allocated, *everything*
        falls back independently, not just the part this method can't see.
        ``self.instance._aligned_offset`` is reset to ``None`` in the same
        place (Codex review round 2, finding 1): a formset re-validated
        against one already-used ``Rack`` instance (a programmatic
        caller, not the admin — Django constructs one formset per
        request) would otherwise leave a *previous* attempt's stashed
        offset in place for ``_resolve_template_offset()`` to reuse if
        *this* attempt's own union search fails — reintroducing the same
        subset-alignment bug finding 2 fixed, by a different route.

        Any advisory the sticky rule recorded on a blank row during its
        own ``full_clean()`` (which always runs before this method) is
        cleared once this method is actually about to decide something —
        after the "nothing to align"/malformed-subnet bails below, not
        before (Codex review round 2, finding 2): a formset that ends up
        aligning nothing must not silently discard advisories a
        programmatic caller had already accumulated from an earlier,
        unrelated ``RackVlanRange.full_clean()`` call — the attribute is
        documented as readable by exactly that kind of caller. Once this
        method *is* deciding something, anything already present was
        necessarily written by a sticky check against *pre-this-
        submission* data (e.g. a sibling range a DELETE checkbox
        elsewhere in this same submission is about to remove), and this
        method's own decision — made with full knowledge of every row's
        fate in this submission — supersedes it: an advisory describing a
        rack that no longer disagrees with itself once the deleted row is
        excluded is worse than none at all.
        """
        rack = self.instance
        rack._aligned_offset_attempted = True
        rack._aligned_offset = None
        anchors: list[tuple[VLAN, str]] = []
        blank_forms = []
        represented_pks: set[int] = set()
        for form in self.forms:
            # Recorded regardless of DELETE/blank below: "represented in
            # this formset" means this saved row *has a form here at all*,
            # not that its form counts as an anchor — a deleted row must
            # still be excluded from the sibling-range fallback query
            # right below, or its still-in-the-database value would sneak
            # back in as an anchor through that second path.
            if form.instance.pk is not None:
                represented_pks.add(form.instance.pk)
            if not form.cleaned_data or form.cleaned_data.get("DELETE", False):
                continue
            vlan = form.cleaned_data.get("vlan")
            if vlan is None:
                continue
            submitted_range = form.cleaned_data.get("address_range")
            if submitted_range:
                anchors.append((vlan, submitted_range))
            else:
                blank_forms.append(form)
        if rack.pk is not None:
            for sibling in rack.vlan_ranges.exclude(pk__in=represented_pks).select_related("vlan"):
                anchors.append((sibling.vlan, sibling.address_range))

        template_vlans = (
            [link.vlan for link in template.vlan_links.select_related("vlan")]
            if template is not None and template_vlan_ids
            else []
        )
        blank_vlans = [form.cleaned_data["vlan"] for form in blank_forms]
        aligned_vlans = template_vlans + blank_vlans
        if not aligned_vlans:
            return  # nothing here for a joint offset to fill in

        vlans_input = []
        for vlan in aligned_vlans:
            info = _vlan_alignment_input(vlan)
            if info is None:
                return  # a malformed subnet; that VLAN's own validation reports it elsewhere
            vlans_input.append((vlan, info))

        # Only cleared once we know we're actually deciding something —
        # see the docstring's finding-2 paragraph for why this can't move
        # any earlier than here.
        rack._range_alignment_advisories.clear()

        def offset_fits_every_vlan(offset: int) -> bool:
            for _vlan, (subnet, used_ranges, dhcp_range) in vlans_input:
                try:
                    candidate_cidr = range_at_offset(subnet, offset, rack.slot_count)
                except ValueError:
                    return False
                if not _candidate_range_is_free(candidate_cidr, subnet, used_ranges, dhcp_range):
                    return False
            return True

        def blank_row_outcomes() -> str:
            """ "<vlan>: <address_range> (offset <n>)" for every blank row,
            joined — what this method actually has to report (decision 3:
            name the outcome, not blame a VLAN) at the moment it falls
            back: each blank row's own already-computed first-fit value,
            in the CIDR-plus-offset vocabulary every ADR 0025 advisory
            uses (``_format_allocation()``, Codex review round 2, finding
            3). Empty when there are no blank rows at all — a
            template-only fallback has nothing here to add, since the
            template's own rows don't exist yet; ``_apply_template()``
            reports those once it creates them.
            """
            return "; ".join(
                f"{form.cleaned_data['vlan']}: "
                f"{_format_allocation(form.instance.address_range, form.cleaned_data['vlan'].subnet)}"
                for form in blank_forms
            )

        anchor_offsets = set()
        for anchor_vlan, anchor_range in anchors:
            try:
                anchor_offsets.add(range_offset(anchor_vlan.subnet, anchor_range))
            except ValueError:
                continue  # malformed anchor value; not this check's job

        offset: int | None = None
        if len(anchor_offsets) == 1:
            candidate_offset = anchor_offsets.pop()
            if offset_fits_every_vlan(candidate_offset):
                offset = candidate_offset
            else:
                outcomes = blank_row_outcomes()
                suffix = f": {outcomes}." if outcomes else "."
                rack._range_alignment_advisories.append(
                    f"This rack's anchored offset ({candidate_offset}) isn't free on every VLAN "
                    f"being allocated here — allocated per VLAN instead{suffix}"
                )
        elif not anchors:
            offset = suggest_aligned_offset([info for _vlan, info in vlans_input], rack.slot_count)
            if offset is None:
                outcomes = blank_row_outcomes()
                suffix = f": {outcomes}." if outcomes else "."
                rack._range_alignment_advisories.append(
                    "No single offset is free on every VLAN this rack is being given a range on "
                    f"— allocated per VLAN instead{suffix}"
                )
        elif len(anchor_offsets) > 1:
            outcomes = blank_row_outcomes()
            suffix = f": {outcomes}." if outcomes else "."
            rack._range_alignment_advisories.append(
                "This rack's existing/entered ranges don't all share one offset, so none could "
                f"be inherited for the new rows — allocated per VLAN instead{suffix}"
            )

        if offset is None:
            return
        rack._aligned_offset = offset
        for form in blank_forms:
            vlan = form.cleaned_data["vlan"]
            form.instance.address_range = range_at_offset(vlan.subnet, offset, rack.slot_count)
            try:
                # "rack" is force-excluded on top of form._get_validation_
                # exclusions(): BaseInlineFormSet.__init__ deliberately
                # re-adds the fk to form._meta.fields ("to make sure
                # validation isn't skipped on that field" — its own
                # comment), which defeats _get_validation_exclusions()'s
                # normal "not one of this form's own fields" skip and
                # leaves exclusion resting on a blank/required inference
                # that isn't reliably true across configurations. Without
                # forcing it, Model.full_clean() validates the FK's raw
                # rack_id — None for a rack that exists only in memory,
                # not yet saved (see _get_related()'s docstring) — and
                # raises a "cannot be null" error that has nothing to do
                # with the address_range value just assigned above.
                exclude = form._get_validation_exclusions() | {"rack"}
                form.instance.full_clean(exclude=exclude)
            except ValidationError as exc:
                form.add_error(None, exc)


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


def _fill_computed_hostname(cleaned_data: dict[str, Any], rack: Any, type_obj: Any) -> list[str]:
    """Fills a blank ``hostname`` via ``assemble_hostname()``/
    ``choose_sequence()`` (ADR 0023 decisions 5 and 7) — a creation-time
    suggestion, never overwriting a typed value. Shared by
    ``NetworkDeviceAddForm``/``NetworkSwitchAddForm``, the same shape as
    ``_fill_rack_derived_owner_default()`` a few lines above, and must run
    before each form's own early return (the trap the plan review
    caught): a spare-pool device has no ``rack`` at all, and ADR 0023
    requires that assembly still work for exactly that case.

    An explicitly-typed ``hostname_sequence`` is honoured, not overridden
    (settled decision 4/6) — ``choose_sequence()`` only runs when it's
    still blank; a typed value instead goes through
    ``resolve_explicit_sequence()``, which only ever bumps *forward* from
    it if that exact name is already taken (code review finding 3 — two
    operators independently typing the same sequence on twin devices
    must not both compute the same name). Either way the resulting value
    is written back into ``cleaned_data``, since it's in every add form's
    ``Meta.fields`` and a newly-created device would otherwise diverge
    from its own name the moment it exists.

    Returns the advisory messages ``choose_sequence()``'s bump produced,
    if any (ADR 0023 decision 7's two messages) — the caller's ``clean()``
    stashes these on ``self._hostname_advisories`` rather than emitting
    them directly, since ``ModelForm.clean()`` has no ``request``
    (review note 6); ``ModelAdmin.save_model()`` emits them once it has
    both.

    ``type_obj`` is whatever object carries ``hostname_slug`` for the
    calling form's own type field — ``switch_type`` for the switch add
    form, or ``device_type.device_model`` for the device one (ADR 0026 PR
    2 moved the field there) — passed in explicitly rather than read by a
    fixed key, since the two forms don't share a field name for it.
    """
    if cleaned_data.get("hostname"):
        return []  # a typed value is never overwritten, and gets no advisories either
    owner = cleaned_data.get("owner")
    type_slug = type_obj.hostname_slug if type_obj is not None else None
    stem_components = HostnameComponents(
        owner_slug=owner.slug if owner is not None else None,
        location_slug=rack.location_slug if rack is not None else None,
        type_slug=type_slug,
        purpose=cleaned_data.get("hostname_purpose") or "",
        sequence=None,
    )
    stem = assemble_hostname(stem_components)
    if stem is None:
        return []  # blocked — nothing to fill, nothing to advise about

    sequence = cleaned_data.get("hostname_sequence")
    advisories: list[str] = []
    sequence_conflict: int | None = None
    if sequence is None:
        bare_twin = _bare_twin_without_sequence(stem, exclude_switch_pk=None, exclude_device_pk=None)
        sequence = choose_sequence(
            stem, purpose=stem_components.purpose, exclude_switch_pk=None, exclude_device_pk=None
        )
        # The starting value only ever reaches 2 via the bare-name branch
        # when a purpose is set (ADR 0023 decision 7, amended again) — for
        # a blank purpose, 1 is tried directly, so a bare twin is never
        # asked to cede it; recommending 1 here would risk naming an
        # already-taken slot on the rare mixed-state stem where a numbered
        # sibling also holds 1.
        if sequence == 2 and bare_twin is not None and stem_components.purpose:
            advisories.append(
                f"Hostname {bare_twin} shares this name with no sequence of its own — consider "
                "giving it hostname_sequence=1."
            )
        cleaned_data["hostname_sequence"] = sequence
    else:
        # Explicit — honoured as typed, only bumped forward if that exact
        # name is already occupied (code review finding 3).
        typed_sequence = sequence
        sequence = resolve_explicit_sequence(stem, sequence, exclude_switch_pk=None, exclude_device_pk=None)
        if sequence != typed_sequence:
            # Silently rewriting the operator's own typed value with no
            # explanation would be exactly the kind of surprise ADR 0019's
            # suggest-don't-lock exists to avoid (code review finding 3).
            sequence_conflict = typed_sequence
            cleaned_data["hostname_sequence"] = sequence

    final_name = assemble_hostname(stem_components._replace(sequence=sequence))
    if sequence_conflict is not None:
        advisories.append(
            f"Requested hostname_sequence {sequence_conflict} was already taken for {final_name} — "
            f"using {sequence} instead."
        )
    # `> 1`, not `is not None`: since hostname_sequence defaults to 1
    # (ADR 0023 decision 7's purpose amendment), a bare 1 is the ordinary
    # outcome rather than a disambiguator, and advising on it would fire
    # for every purposeless racked device. MORE_MUSINGS' rule is about
    # *avoiding collisions* — only a number forced above 1 represents one.
    if (
        rack is not None
        and rack.location_slug
        and sequence is not None
        and sequence > 1
        and not stem_components.purpose
    ):
        advisories.append(
            f"A purpose reads better than a bare number for {final_name} — consider setting hostname_purpose."
        )
    if final_name:
        cleaned_data["hostname"] = final_name
    return advisories


class _HostnameRecomputeResult(NamedTuple):
    """One row's outcome from the "Recompute hostname" action — enough for
    ``recompute_hostnames()`` to both report and, for ``skipped``, say why.
    """

    status: str  #: "renamed" | "unchanged" | "skipped"
    reason: str | None  #: populated only for "skipped"
    advisories: list[str]


def _bare_twin_without_sequence(
    stem: str, *, exclude_switch_pk: int | None, exclude_device_pk: int | None
) -> "NetworkSwitch | NetworkDevice | None":
    """The first row (switch or device) whose stored ``hostname`` is
    exactly ``stem`` and whose ``hostname_sequence`` is null, if any —
    what the first advisory (ADR 0023 decision 7, amended) names: *"a
    twin exists with no sequence, recommend assigning it 1."* A message
    about an already-saved row, not an action on it — this never writes
    anything.
    """
    switches = NetworkSwitch.objects.filter(hostname=stem, hostname_sequence__isnull=True)
    if exclude_switch_pk is not None:
        switches = switches.exclude(pk=exclude_switch_pk)
    found = switches.first()
    if found is not None:
        return found
    devices = NetworkDevice.objects.filter(hostname=stem, hostname_sequence__isnull=True)
    if exclude_device_pk is not None:
        devices = devices.exclude(pk=exclude_device_pk)
    return devices.first()


def _recompute_hostname(
    obj: "NetworkSwitch | NetworkDevice",
    *,
    type_slug: str | None,
    exclude_switch_pk: int | None,
    exclude_device_pk: int | None,
) -> _HostnameRecomputeResult:
    """The "Recompute hostname" action's per-object logic (ADR 0023
    decision 5), shared by both admins — mutates ``obj`` in place
    (``owner``, ``hostname_sequence``, ``hostname``) and leaves saving it
    to the caller, exactly as ``pull_cards`` leaves its own ``save()`` to
    the per-row loop that calls it.

    1. A blank ``owner`` is filled from ``obj.rack.owner`` when this
       object has a rack — the add-form default never fired for
       already-imported rows, so without this every production device
       stays permanently blocked on a null owner (ADR 0023 decision 5).
       Stored regardless of what happens next, unlike ``assemble_hostname()``
       itself, which never reads through to the rack for owner.
    2. ``assemble_hostname()`` over the stem (every component but
       sequence). Blocked (``None``) is reported, naming which component
       is missing, and nothing else below runs.
    3. ``choose_sequence()`` only when ``hostname_sequence`` is still
       null — an explicitly-set value is honoured, never overridden
       (settled decision 4/6), though still passed through
       ``resolve_explicit_sequence()`` to bump forward if that exact name
       is already taken (code review finding 3).
    4. The final name overwrites whatever ``hostname`` held, unconditionally.
    """
    rack = obj.rack  # None for a spare-pool object; one query either way, needed for location below
    if obj.owner_id is None and rack is not None:
        obj.owner = rack.owner  # rack.owner may itself be None; still "stored" (assigned in memory)
    owner = obj.owner
    stem_components = HostnameComponents(
        owner_slug=owner.slug if owner is not None else None,
        location_slug=rack.location_slug if rack is not None else None,
        type_slug=type_slug,
        purpose=obj.hostname_purpose,
        sequence=None,
    )
    stem = assemble_hostname(stem_components)
    if stem is None:
        missing = [
            name
            for name, present in (("owner", stem_components.owner_slug), ("type's hostname_slug", type_slug))
            if not present
        ]
        return _HostnameRecomputeResult("skipped", f"missing {' and '.join(missing)}", [])

    advisories: list[str] = []
    sequence_conflict: int | None = None
    if obj.hostname_sequence is None:
        bare_twin = _bare_twin_without_sequence(
            stem, exclude_switch_pk=exclude_switch_pk, exclude_device_pk=exclude_device_pk
        )
        # current_name=obj.hostname (code review finding 1) — without this,
        # the bare-named member of a numbered group is not idempotent:
        # excluding itself from the sibling scan makes the highest
        # *remaining* sibling look like the group's own top, so it gets
        # bumped to a numbered suffix on every subsequent recompute.
        obj.hostname_sequence = choose_sequence(
            stem,
            purpose=obj.hostname_purpose,
            current_name=obj.hostname,
            exclude_switch_pk=exclude_switch_pk,
            exclude_device_pk=exclude_device_pk,
        )
        # The starting value only ever reaches 2 via the bare-name branch
        # when a purpose is set (ADR 0023 decision 7, amended again) — for
        # a blank purpose, 1 is tried directly, so a bare twin is never
        # asked to cede it; recommending 1 here would risk naming an
        # already-taken slot on the rare mixed-state stem where a numbered
        # sibling also holds 1.
        if obj.hostname_sequence == 2 and bare_twin is not None and obj.hostname_purpose:
            advisories.append(
                f"Hostname {bare_twin} shares this name with no sequence of its own — consider "
                "giving it hostname_sequence=1."
            )
    else:
        # Explicit — honoured as-is, only bumped forward if that exact
        # name is already taken (code review finding 3).
        original_sequence = obj.hostname_sequence
        obj.hostname_sequence = resolve_explicit_sequence(
            stem,
            obj.hostname_sequence,
            exclude_switch_pk=exclude_switch_pk,
            exclude_device_pk=exclude_device_pk,
        )
        if obj.hostname_sequence != original_sequence:
            sequence_conflict = original_sequence

    final_name = assemble_hostname(stem_components._replace(sequence=obj.hostname_sequence))
    # stem (owner + type_slug at minimum) already assembled above; adding a
    # sequence on top of already-present components can't newly block.
    assert final_name is not None
    if sequence_conflict is not None:
        advisories.append(
            f"Requested hostname_sequence {sequence_conflict} was already taken for {final_name} — "
            f"using {obj.hostname_sequence} instead."
        )
    if (
        rack is not None
        and rack.location_slug
        and obj.hostname_sequence is not None
        # See the matching comment on the add-form path above: a bare 1 is
        # now the default, so only a number forced above it means a
        # collision the purpose field could have avoided.
        and obj.hostname_sequence > 1
        and not obj.hostname_purpose
    ):
        advisories.append(
            f"A purpose reads better than a bare number for {final_name} — consider setting hostname_purpose."
        )
    if obj.hostname == final_name:
        return _HostnameRecomputeResult("unchanged", None, advisories)
    obj.hostname = final_name
    return _HostnameRecomputeResult("renamed", None, advisories)


def _emit_hostname_advisories(request: HttpRequest, form: object) -> None:
    """Emits whatever a form stashed on ``form._hostname_advisories`` —
    originally only ``_fill_computed_hostname()``'s two messages (ADR
    0023 decision 7), called from each admin's ``save_model()``, which is
    the first place both a ``request`` and the form exist together
    (review note 6). ``NetworkDeviceChangeForm`` now stashes here too
    (ADR 0024 plan settled decision 6) — the over-31 length advisory,
    computed in ``_post_clean()`` rather than assembled in ``clean()``,
    but surfaced through this same list and the same ``messages.info``
    level either way.

    ``getattr(..., [])`` covers every form that never stashed anything at
    all — a bare ``ModelForm`` constructed directly rather than through
    the admin (no ``save_model()`` call reaches it either, so this never
    even runs for one), and an add form whose ``clean()`` bailed before
    reaching ``_fill_computed_hostname()``.
    """
    for advisory in getattr(form, "_hostname_advisories", []):
        messages.info(request, advisory)


def _emit_dante_warnings(request: HttpRequest, form: object) -> None:
    """Emits whatever ``_post_clean()`` stashed on ``form._dante_warnings``
    (ADR 0024 plan settled decision 8's rename warning) — ``messages.
    warning``, not ``messages.info``: this names a hazard (an audio
    outage), not a suggestion. Called from ``NetworkDeviceAdmin.
    save_model()`` beside ``_emit_hostname_advisories()``.

    ``getattr(..., [])`` covers the add form, which never stashes this at
    all (no rename warning on creation — settled decision 8, nothing
    exists yet to re-subscribe), and any bare ``ModelForm`` built outside
    the admin.

    Deferred to ``transaction.on_commit()`` (Codex review finding 2) —
    unlike ``_emit_hostname_advisories()`` above, which still fires
    immediately and is deliberately left that way. Django's admin
    ``_changeform_view()`` wraps ``save_model()`` and ``save_related()``
    (which is what actually saves ``NetworkDevicePortInline``, present on
    this very page) in **one** ``transaction.atomic()`` block. A device
    can save cleanly here and then have the ports formset raise —
    ``AuditedModelAdminMixin.changeform_view()`` catches that and
    redirects with an error, but Django's message queue is not
    transactional, so a warning emitted immediately would still reach the
    operator for a rename that never committed. That is the worst
    possible direction for *this* message specifically to be wrong in: it
    instructs someone to go re-point live Dante routing at a name the
    database doesn't actually hold. ``on_commit()`` is a no-op wrapper
    (fires immediately) outside an atomic block, so this changes nothing
    for the recompute action's own already-self-contained per-row
    transaction, or for any other caller.

    The identical risk on ``_emit_hostname_advisories()`` is judged not
    worth the same treatment: "consider setting hostname_purpose"
    surviving a rolled-back save is cosmetic, not an instruction to touch
    production audio equipment, so that phase-18 path is left as it was.
    """
    for warning in getattr(form, "_dante_warnings", []):
        # functools.partial, not a lambda closing over the loop variable
        # — a plain closure would see whatever `warning` is by the time
        # commit fires (always the last one, for more than one warning in
        # the list), since Python closures bind the variable, not its
        # value at closure-creation time. partial() binds the value
        # immediately as a bound argument instead.
        transaction.on_commit(functools.partial(messages.warning, request, warning))


def _suggest_dante_unit_id() -> UnitIdSuggestion | None:
    """``suggest_unit_id()`` (ADR 0024 decision 4), backed by the query
    the pure function itself can't make (settled decision 3 — ``dante.py``
    is pure; the admin does the query). One aggregate query for the
    common case — SQL ``MAX()`` already ignores nulls, so no separate
    ``isnull`` exclude is needed — and the full assigned-ID set is
    fetched only once the highest reaches 127, the one case
    ``suggest_unit_id()`` needs more than the highest value to answer.
    """
    highest = NetworkDevice.objects.aggregate(Max("dante_unit_id"))["dante_unit_id__max"]
    if highest is None or highest < dante.DANTE_UNIT_ID_MAX:
        return dante.suggest_unit_id([] if highest is None else [highest])
    # The `.exclude(isnull=True)` above already guarantees no None reaches
    # here — the `if value is not None` filter is what lets mypy narrow
    # the column's nullable int type to plain int, not a second check.
    assigned: list[int] = [
        value
        for value in NetworkDevice.objects.exclude(dante_unit_id__isnull=True).values_list(
            "dante_unit_id", flat=True
        )
        if value is not None
    ]
    return dante.suggest_unit_id(assigned)


class NetworkDeviceTypeChoiceField(forms.ModelChoiceField):
    """The device-type picker's label, carrying the model's description
    (ADR 0026 decision 6) — ``str(type)`` plus `` (description)``, and
    plain ``str(type)`` when the description is blank (no empty
    parentheses), so 15 of today's 22 models render exactly as they do
    now.

    One declaration covers every picker (PLAN-adr-0026.md settled decision
    1): the ordinary add form below installs it via ``Meta.field_classes``,
    and the two card-fit call sites (``_render_fit_card_page``,
    ``_fit_new_card``) only ever reassign ``.queryset`` on an
    already-constructed field, which preserves the field instance's class.
    ``admin.py`` has no ``label_from_instance``/``formfield_for_foreignkey``
    anywhere else, and ``device_type`` is read-only on change (fixed at
    creation, ADR 0010), so this is the whole picker surface.
    """

    def label_from_instance(self, obj: NetworkDeviceType) -> str:
        label = str(obj)
        description = obj.device_model.description
        return f"{label} ({description})" if description else label


class NetworkDeviceAddForm(forms.ModelForm):
    """Carries the creation-time-only ``port_addressing`` choice (ADR 0013)
    — not a model field, so it can't be expressed via ``Meta.fields``
    alone. Used only for the add view (``NetworkDeviceAdmin.get_form()``);
    the choice has no effect after creation, so the change form omits it
    entirely rather than showing a field that does nothing.

    ``clean()`` fills a blank ``rack_slot`` with the lowest ordinal at
    which every one of the chosen type's ``claimed_offsets`` is free (ADR
    0027's ``lowest_free_placement()`` — the placement suggester for a
    device claiming a *set* of ordinals rather than a contiguous span, the
    fix for issue #62/#83's suggestion half).
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
        # owner/hostname_purpose/hostname_sequence (ADR 0023, plan settled
        # decision 2) and dante_unit_id (ADR 0024 plan settled decision 2)
        # must be listed here explicitly — this is an explicit list, not
        # exclude=[], so construct_instance() silently drops any field
        # left out of it, including the rack-derived owner default
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
            "dante_unit_id",
        ]
        # ADR 0026 settled decision 1 — the ordinary picker's hook for the
        # model description in the dropdown label.
        field_classes = {"device_type": NetworkDeviceTypeChoiceField}

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # ADR 0026 — the description in every dropdown label reads
        # device_type.device_model, so this must be select_related or the
        # dropdown is an N+1 across every device type.
        self.fields["device_type"].queryset = NetworkDeviceType.objects.select_related(  # type: ignore[attr-defined]
            "device_model"
        )

    def clean(self) -> dict[str, Any]:
        """A blank ``rack_slot`` is filled in with the lowest ordinal at
        which the chosen type's ``claimed_offsets`` are all free (ADR
        0027's ``lowest_free_placement()`` — the placement suggester for a
        device claiming a *set* of ordinals rather than a contiguous span,
        the fix for issue #62/#83's suggestion half) — never overwriting a
        typed value.

        Bails outright unless both ``rack`` and ``device_type`` cleaned: a
        field error on either means the placement search is unknowable, so
        the ordinary field error is left to surface on its own.
        """
        cleaned_data = super().clean() or {}
        rack = cleaned_data.get("rack")
        _fill_rack_derived_owner_default(cleaned_data, rack)
        device_type = cleaned_data.get("device_type")
        # Must run before the "rack is None" bail immediately below (Codex
        # review of the plan, note 7) — a spare-pool device has no rack at
        # all, and ADR 0023 requires that assembly still work for exactly
        # that case (location is simply absent, not blocking). Stashed
        # rather than emitted — clean() has no request (review note 6);
        # save_model() emits these once it has one.
        # ADR 0026 PR 2 — hostname_slug moved off device_type onto its
        # device_model FK, so _fill_computed_hostname() (shared with the
        # switch add form, which still passes switch_type directly) is
        # handed the model, not the profile.
        self._hostname_advisories = _fill_computed_hostname(
            cleaned_data, rack, device_type.device_model if device_type is not None else None
        )
        if rack is None or device_type is None:
            return cleaned_data
        host_slot = cleaned_data.get("rack_slot")
        if host_slot is None:
            occupied = occupied_rack_slot_ordinals(rack)
            host_slot = lowest_free_placement(occupied, device_type.claimed_offsets, rack.slot_count)
            if host_slot is None:
                self.add_error(
                    "rack_slot",
                    f"No free rack slot for {device_type}'s claimed ordinals "
                    f"({sorted(device_type.claimed_offsets)} from the slot) in {rack} "
                    f"(slot_count {rack.slot_count}).",
                )
                return cleaned_data
            cleaned_data["rack_slot"] = host_slot
        return cleaned_data

    def _post_clean(self) -> None:
        # `or`, not `.get(..., default)` alone — required=False means an
        # omitted/blank submission cleans to "" (present in cleaned_data, not
        # absent), and a bare `.get()` default only covers a missing key.
        # `or` covers both that and the ChoiceField-rejected-value case (which
        # does leave the key genuinely absent). Must run before
        # super()._post_clean(), which is what calls self.instance.full_clean().
        self.instance.port_addressing = self.cleaned_data.get("port_addressing") or PortAddressing.STATIC
        super()._post_clean()  # type: ignore[misc]
        # ADR 0024 plan settled decision 6, review note 1 — measured here,
        # off the fully-normalized self.instance super()._post_clean() just
        # produced (construct_instance() then instance.full_clean(), which
        # lowercases hostname), not in clean()'s un-normalized cleaned_data.
        # Appended to clean()'s own _fill_computed_hostname() advisories
        # rather than replacing them — both surface through the same list,
        # at the same messages.info level, via _emit_hostname_advisories().
        # No rename warning on creation (settled decision 8): nothing
        # exists yet to re-subscribe.
        if self.instance.dante_unit_id is None:
            advisory = dante.over_length_advisory(self.instance.hostname)
            if advisory is not None:
                self._hostname_advisories.append(advisory)


class NetworkDeviceChangeForm(forms.ModelForm):
    """Change form for an existing device — deliberately distinct from
    ``NetworkDeviceAddForm`` (same shape as ``NetworkSwitchAddForm``/the
    default ``ModelForm`` split elsewhere here): ``port_addressing`` only
    makes sense at creation, so the change form omits it entirely rather
    than showing a field that does nothing.

    ``_post_clean()`` (ADR 0024 plan settled decisions 6 and 8, review
    note 1) is new here — this form had neither ``clean()`` nor
    ``_post_clean()`` before. Both the over-31 advisory and the Dante
    rename warning are computed **after** ``super()._post_clean()``, off
    the fully-normalized ``self.instance``, against a fresh query for the
    row as it's stored right now — never against ``self.instance`` before
    normalization, which is un-lowercased and would false-positive a
    rename warning on a case-only edit (review note 1's finding).
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
            "dante_unit_id",
        ]
        # dante_device_name isn't a model field — Django's admin only
        # pulls a readonly field's help text from an actual model field
        # (django.contrib.admin.utils.help_text_for_field), so the ADR's
        # verbatim text is supplied here instead; AdminReadonlyField
        # checks form._meta.help_texts before falling back to that.
        help_texts = {
            "dante_device_name": (
                "The name to set in Dante Controller. Dante routes audio by this name, so "
                "changing it drops audio until subscriptions are rebuilt — and a name that was "
                "previously in use will pull audio from whatever now holds it."
            ),
        }

    def _post_clean(self) -> None:
        super()._post_clean()  # type: ignore[misc]
        self._hostname_advisories: list[str] = []
        self._dante_warnings: list[str] = []
        if self.instance.pk is None:
            return  # defensive only — this form is never used to create
        stored = NetworkDevice.objects.filter(pk=self.instance.pk).values("hostname", "dante_unit_id").first()
        if stored is None:
            return  # row vanished under us; save() will fail its own way
        old_hostname = stored["hostname"]
        old_unit_id = stored["dante_unit_id"]
        new_unit_id = self.instance.dante_unit_id
        # Advisory: NetworkDevice-only, only where the unit ID is null
        # (settled decision 6) — unconditional on whether the hostname
        # itself changed this submission, the same posture the model's
        # own blocking check takes for a unit-ID device.
        if new_unit_id is None:
            advisory = dante.over_length_advisory(self.instance.hostname)
            if advisory is not None:
                self._hostname_advisories.append(advisory)
        # Rename warning: fires whenever this save changes the *Dante*
        # name of a device that carries (or carried) a unit ID on either
        # side — editing dante_unit_id, editing hostname while a unit ID
        # is set, or setting/clearing the unit ID itself (settled
        # decision 8's table) — **and** there was an actual previous
        # Dante name to lose. Three conditions, all required:
        #
        # - ``old_unit_id is not None or new_unit_id is not None`` — the
        #   tool cannot know it's a Dante device at all when neither side
        #   ever carried an ID (a plain hostname rename must stay silent).
        # - ``old_name is not None`` — a unit-ID device whose hostname was
        #   *always* blank never had anything for Dante to route by, so
        #   there is nothing to re-subscribe; same "commissioning, not an
        #   outage" reasoning settled decision 8 already gives creation,
        #   extended to the equivalent case reached by editing instead.
        # - ``old_name != new_name`` — no-op, unrelated-field and
        #   case-only edits must not warn (review note 1).
        old_name = dante.dante_device_name(old_unit_id, old_hostname)
        new_name = self.instance.dante_device_name
        if (
            (old_unit_id is not None or new_unit_id is not None)
            and old_name is not None
            and old_name != new_name
        ):
            # Labelled by the *old* hostname, not the post-rename one
            # (ADR 0024's own pinned example: "mps-stage-rio-1 is a Dante
            # device … Its Dante name is now `Y001-mps-stage-rio-2`") —
            # Dante Controller still shows the old name until the
            # operator acts on this warning, so that's the identity an
            # operator can actually go find. __str__'s own "Device #pk"
            # fallback, replicated here since self.instance's blank-
            # hostname fallback would otherwise read the *new* pk-less
            # state.
            old_label = old_hostname or f"Device #{self.instance.pk}"
            self._dante_warnings.append(
                dante.rename_warning(
                    old_label,
                    old_unit_id=old_unit_id,
                    new_unit_id=new_unit_id,
                    old_name=old_name,
                    new_name=new_name,
                )
            )


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
        # Stashed rather than emitted — see NetworkDeviceAddForm.clean()'s
        # identical comment.
        self._hostname_advisories = _fill_computed_hostname(
            cleaned_data, rack, cleaned_data.get("switch_type")
        )
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
        # location_slug/owner (ADR 0023) must be listed here explicitly —
        # this is an explicit list, not exclude=[], so construct_instance()
        # silently drops any field left out of it (the same trap
        # NetworkDeviceAddForm's Meta.fields carries).
        fields = ["name", "slot_count", "owner", "location_slug"]

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
    fields = [
        "port_number",
        "description",
        "port_type",
        "vlan",
        "slot_offset",
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
            return ["port_number", "description", "port_type", "vlan", "slot_offset"]
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


class RackRangeOffsetsDivergeFilter(admin.SimpleListFilter):
    """ADR 0025 decision 5 — copied from ``_HostnameDivergesFilterBase``
    (`:1774` below), since ``Rack.range_offsets_diverge`` is a Python
    property, not a database column, and can't be an ordinary
    ``list_filter`` string entry either. Scans in Python — 21 racks in
    production today — with ``vlan_ranges__vlan`` prefetched here so the
    scan itself stays one query rather than an N+1 across the
    changelist's own row count; ``RackAdmin.get_queryset()`` already
    carries this same prefetch unconditionally for the ordinary
    changelist, but the filter doesn't rely on that — it applies its own,
    the same defensive posture ``_HostnameDivergesFilterBase`` takes.
    """

    title = "range offset divergence"
    parameter_name = "range_offsets_diverge"

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Diverges"), ("no", "Matches")]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        value = self.value()
        if value not in ("yes", "no"):
            return queryset
        wants_diverging = value == "yes"
        matching_pks = [
            obj.pk
            for obj in queryset.prefetch_related("vlan_ranges__vlan")
            if obj.range_offsets_diverge == wants_diverging
        ]
        return queryset.filter(pk__in=matching_pks)


@admin.register(Rack)
class RackAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slot_count", "owner", "location_slug", "range_offsets_diverge"]
    search_fields = ["name", "location_slug"]
    list_filter = ["owner", RackRangeOffsetsDivergeFilter]
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

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        # ADR 0025 — range_offsets_diverge (rendered by the column below)
        # reads every range's own vlan. Unconditional here, not left to
        # RackRangeOffsetsDivergeFilter (which only prefetches when a
        # filter value is actually selected), so the ordinary changelist
        # — no filter applied — doesn't N+1 across the rack list.
        return super().get_queryset(request).prefetch_related("vlan_ranges__vlan")

    @admin.display(boolean=True, description="Offsets diverge")
    def range_offsets_diverge(self, obj: Rack) -> bool:
        return obj.range_offsets_diverge

    def save_model(self, request: HttpRequest, obj: Any, form: object, change: bool) -> None:
        super().save_model(request, obj, form, change)
        # ADR 0025 — whatever any of the three allocation paths recorded
        # on this exact rack instance during formset/model validation
        # (the inline formset's joint offset, RackVlanRange.clean()'s
        # sticky rule, and _apply_template()'s own fallback, all reachable
        # from this one save) — same messages.info level and same
        # "admin-only, programmatic callers read the attribute instead"
        # posture as _emit_hostname_advisories().
        for advisory in obj._range_alignment_advisories:
            messages.info(request, advisory)


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


class _HostnameDivergesFilterBase(admin.SimpleListFilter):
    """#54 / ADR 0023 phase 18 PR 4 — ``hostname_diverges`` is a Python
    property, not a database column, so it can't be an ordinary
    ``list_filter`` string entry (this repo has no existing
    ``SimpleListFilter`` to copy; this is the first). ``queryset()`` scans
    in Python rather than filtering in SQL — 84 equipment rows today, and
    every relation the property reads (``owner``, ``rack``, the Type) is
    ``select_related`` here so the scan itself stays one query rather than
    an N+1 across the changelist's own row count.

    ``_type_field`` names the ``select_related()`` path to whatever the
    property actually reads, since that differs between the two concrete
    subclasses below — ``switch_type`` still carries its own
    ``hostname_slug``, but ``device_type`` doesn't since ADR 0026 PR 2, so
    that one names ``device_type__device_model`` instead. Everything else
    is identical.
    """

    title = "hostname divergence"
    parameter_name = "hostname_diverges"
    _type_field: str = ""

    def lookups(self, request: HttpRequest, model_admin: Any) -> list[tuple[str, str]]:
        return [("yes", "Diverges"), ("no", "Matches")]

    def queryset(self, request: HttpRequest, queryset: QuerySet) -> QuerySet:
        value = self.value()
        if value not in ("yes", "no"):
            return queryset
        wants_diverging = value == "yes"
        matching_pks = [
            obj.pk
            for obj in queryset.select_related("owner", "rack", self._type_field)
            if obj.hostname_diverges == wants_diverging
        ]
        return queryset.filter(pk__in=matching_pks)


class NetworkSwitchHostnameDivergesFilter(_HostnameDivergesFilterBase):
    _type_field = "switch_type"


class NetworkDeviceHostnameDivergesFilter(_HostnameDivergesFilterBase):
    # "device_type__device_model", not "device_type" — ADR 0026 PR 2 moved
    # hostname_slug onto device_model, so hostname_diverges now traverses
    # one more FK; select_related() accepts the dotted path directly, and
    # _type_field is used nowhere except that call.
    _type_field = "device_type__device_model"


class NetworkDeviceTypeRelatedFilter(admin.RelatedFieldListFilter):
    """Codex review of ADR 0026's PR 1 — ``RelatedFieldListFilter`` builds
    its sidebar choices via ``Field.get_choices()``, a queryset entirely
    separate from ``ModelAdmin.get_queryset()`` — ``list_select_related``
    on ``NetworkDeviceAdmin`` never touches it. Rendering the "By device
    type" filter therefore called ``str(type)`` (which now dereferences
    ``device_model``) once per ``NetworkDeviceType`` row with no join at
    all — an N+1 on every changelist page load, not just once.

    ``field_choices()`` is the hook ``RelatedFieldListFilter.__init__``
    actually calls; ``Field.get_choices()`` itself has no queryset-
    injection parameter in this Django version, so this reimplements it
    (mirroring ``Field.get_choices()``'s own body) rather than trying to
    wrap it. ``device_type``'s target field is the model's plain ``pk``
    (no ``to_field``), so ``x.pk`` is correct without needing
    ``get_related_field()``'s extra indirection.
    """

    def field_choices(self, field, request, model_admin):
        ordering = self.field_admin_ordering(field, request, model_admin)
        queryset = field.remote_field.model._default_manager.complex_filter(
            field.get_limit_choices_to()
        ).select_related("device_model")
        if ordering:
            queryset = queryset.order_by(*ordering)
        return [(obj.pk, str(obj)) for obj in queryset]


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
    list_filter = ["rack", "switch_type", "owner", NetworkSwitchHostnameDivergesFilter]
    # Declarative, not relied-on auto-select_related() — owner/rack/switch_type
    # are all nullable-or-not-descended the same way VLANAdmin's comment
    # above explains.
    list_select_related = ["switch_type", "rack", "owner"]
    inlines = [NetworkSwitchAddressInline, NetworkSwitchPortInline]
    show_auditlog_history_link = True
    # "recompute_hostnames" must be named here explicitly (settled decision
    # 8) — a decorated-but-unlisted admin.action() method never renders.
    actions = [delete_selected, "recompute_hostnames"]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # switch_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is not None:
            return ["switch_type"]
        return []

    def save_model(self, request: HttpRequest, obj: Any, form: object, change: bool) -> None:
        super().save_model(request, obj, form, change)
        _emit_hostname_advisories(request, form)

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

    # -- Recompute hostname (ADR 0023) -----------------------------------------

    @admin.action(permissions=["change"], description="Recompute hostname")
    def recompute_hostnames(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Saved per row, like ``pull_cards`` — each row is locked,
        recomputed and saved on its own, sequentially, so a batch of
        identical switches gets that many distinct names rather than one
        query's worth of stale sibling data (settled decision, PR 3
        Tests: 17 identical amps -> 17 distinct names, mirrored here for
        switches). ``permissions=["change"]`` restricts this to holders of
        ``inventory.change_networkswitch`` — Django's own
        ``_filter_actions_by_permissions``, not a hand-rolled check.
        """
        renamed = 0
        unchanged = 0
        skipped: list[str] = []
        for switch in queryset:
            with transaction.atomic():
                current = NetworkSwitch.objects.select_for_update().filter(pk=switch.pk).first()
                if current is None:
                    continue
                result = _recompute_hostname(
                    current,
                    type_slug=current.switch_type.hostname_slug,
                    exclude_switch_pk=current.pk,
                    exclude_device_pk=None,
                )
                current.save()
                if result.status == "skipped":
                    skipped.append(f"{current} ({result.reason})")
                elif result.status == "renamed":
                    renamed += 1
                else:
                    unchanged += 1
                for advisory in result.advisories:
                    messages.info(request, advisory)
        if renamed:
            messages.success(request, f"Recomputed {renamed} hostname(s).")
        if unchanged:
            messages.info(request, f"{unchanged} hostname(s) already up to date.")
        if skipped:
            messages.warning(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}.")


@admin.register(NetworkDeviceModel)
class NetworkDeviceModelAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    """ADR 0026 — the bare hardware identity. Unlike ``NetworkDeviceTypeAdmin``
    below, no ``get_readonly_fields`` override: decision 3 makes this row
    editable even once its profiles have instances — the whole point of
    the extraction is that correcting a manufacturer/model string here
    updates every profile of that model coherently.
    """

    # hostname_slug joined this model in ADR 0026 PR 2 — moved off
    # NetworkDeviceType, same reasoning as description (ADR 0026 decision
    # 3): a model-level fact, not a profile-level one.
    list_display = ["manufacturer", "model", "description", "hostname_slug"]
    search_fields = ["manufacturer", "model", "description", "hostname_slug"]
    show_auditlog_history_link = True


@admin.register(NetworkDeviceType)
class NetworkDeviceTypeAdmin(AuditedModelAdminMixin, AuditlogHistoryAdminMixin, admin.ModelAdmin):
    # hostname_slug is not a column here since ADR 0026 PR 2 — see
    # NetworkDeviceModelAdmin, above.
    list_display = ["device_model", "name", "port_count", "is_add_in_card"]
    list_filter = ["is_add_in_card"]
    # Not "device_model" (Codex review note 3) — a bare FK name here
    # becomes `device_model__icontains`, which raises `FieldError:
    # Unsupported lookup 'icontains' for ForeignKey or join on the field
    # not permitted` the moment an operator types in the search box.
    # Verified against this tree.
    search_fields = ["device_model__manufacturer", "device_model__model", "name"]
    list_select_related = ["device_model"]
    inlines = [NetworkDeviceTypePortInline]
    show_auditlog_history_link = True

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # ADR 0026 decision 3 replaces ADR 0010's lock on
        # manufacturer/model with a lock on the device_model FK itself —
        # NetworkDeviceType.save()/clean() lock it identically, via
        # _locked_snapshot(). is_add_in_card joins the lock (ADR 0022 PR 3)
        # — flipping it after instances exist would either strand fitted
        # devices or retroactively offer ordinary equipment to the fit
        # picker.
        if _profile_locked(obj, "devices"):
            return ["device_model", "name", "port_count", "is_add_in_card"]
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
        # ADR 0024 plan settled decision 9 — sparse (2 of 61 devices today)
        # but "which IDs are taken" is the question an operator actually
        # has; the derived name stays off the changelist and only renders
        # on the detail surfaces, where it has an object to read from.
        "dante_unit_id",
    ]
    search_fields = ["hostname", "serial_number"]
    # ("host", EmptyFieldListFilter) gives fitted/unfitted as the filter
    # choices (ADR 0022 PR 3) — a plain "host" filter would instead list
    # every individual host, which isn't the question this filter answers.
    # ("device_type", NetworkDeviceTypeRelatedFilter) — Codex review of
    # ADR 0026's PR 1: the sidebar's own choices queryset is separate from
    # list_select_related below and needs its own select_related, or the
    # filter dropdown is an N+1 across every NetworkDeviceType row.
    list_filter = [
        "rack",
        ("device_type", NetworkDeviceTypeRelatedFilter),
        ("host", admin.EmptyFieldListFilter),
        "owner",
        NetworkDeviceHostnameDivergesFilter,
    ]
    # device_type__device_model (ADR 0026 review note 4) — __str__ now
    # dereferences device_model, so a bare "device_type" leaves an N+1
    # across every row once the changelist renders it.
    list_select_related = ["device_type__device_model", "rack", "host", "owner"]
    inlines = [NetworkDevicePortInline]
    show_auditlog_history_link = True
    # "recompute_hostnames" must be named here explicitly (settled decision
    # 8, matching "pull_cards" already here) — a decorated-but-unlisted
    # admin.action() method never renders.
    actions = ["pull_cards", "recompute_hostnames"]

    def get_readonly_fields(self, request: HttpRequest, obj: Any = None) -> list[str]:
        # device_type is fixed at creation (ADR 0010) — editable on Add,
        # locked on every subsequent Change.
        if obj is None:
            return []
        readonly = ["device_type"]
        # ADR 0024 plan settled decision 9 — only for an existing object
        # that carries a unit ID; dante_device_name reads null when there
        # isn't one (decision 1's table), and showing it unconditionally
        # would assert Dante membership this tool cannot establish.
        if obj.dante_unit_id is not None:
            readonly.append("dante_device_name")
        return readonly

    def save_model(self, request: HttpRequest, obj: Any, form: object, change: bool) -> None:
        super().save_model(request, obj, form, change)
        _emit_hostname_advisories(request, form)
        _emit_dante_warnings(request, form)

    def get_form(self, request: HttpRequest, obj: Any = None, change: bool = False, **kwargs: Any) -> Any:
        # port_addressing (ADR 0013) only makes sense at creation. Two
        # distinct forms, same shape as NetworkSwitchAddForm/the default
        # ModelForm split elsewhere here.
        if obj is None:
            kwargs["form"] = NetworkDeviceAddForm
        else:
            kwargs["form"] = NetworkDeviceChangeForm
        form_class = super().get_form(request, obj, change=change, **kwargs)
        self._append_dante_unit_id_suggestion(form_class)
        return form_class

    @staticmethod
    def _append_dante_unit_id_suggestion(form_class: Any) -> None:
        """Appends the live "next free unit ID" suggestion to
        ``dante_unit_id``'s help text — displayed, never written (ADR
        0024 plan settled decision 2): every other suggester in this
        codebase fills a blank field in ``clean()``, but blank means "not
        controlled by a Yamaha console" here, and filling it would hand a
        unit ID to consoles that must never carry one (decision 6).

        Mutates only ``form_class.base_fields`` — the class
        ``super().get_form()`` just built via ``modelform_factory()``,
        never ``NetworkDeviceAddForm``/``NetworkDeviceChangeForm``'s own
        ``base_fields`` directly. That distinction is what keeps this
        safe to call on every request (review note 4): ``modelform_
        factory()``'s metaclass rebuilds ``base_fields`` with fresh field
        instances each call, so appending to *that* copy can't leak
        across requests, but appending to the two form classes' own
        ``base_fields`` would grow the help string by one sentence per
        page load.
        """
        field = form_class.base_fields.get("dante_unit_id")
        if field is None:
            return
        suggestion = _suggest_dante_unit_id()
        if suggestion is None:
            field.help_text += " All 127 unit IDs are in use."
        elif suggestion.reclaimed:
            field.help_text += (
                f" Allocation has reached 127, so the next suggestion is {suggestion.value} — a gap, "
                "which may have been used before. Check what last held "
                f"Y0{suggestion.value:02X}- before using it, or audio may route to the wrong box."
            )
        else:
            field.help_text += f" Next free unit ID: {suggestion.value}."

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

    # -- Recompute hostname (ADR 0023) -----------------------------------------

    @admin.action(permissions=["change"], description="Recompute hostname")
    def recompute_hostnames(self, request: HttpRequest, queryset: QuerySet) -> None:
        """Saved per row, like ``pull_cards`` above — each row is locked,
        recomputed and saved on its own, sequentially, so a batch of
        identical devices gets that many distinct names rather than one
        stale round of sibling data (PR 3 Tests: 17 identical amps -> 17
        distinct names). ``permissions=["change"]`` restricts this to
        holders of ``inventory.change_networkdevice`` — Django's own
        ``_filter_actions_by_permissions``, not a hand-rolled check.

        ADR 0024 plan settled decisions 7 and 8 add two things this
        method's ``NetworkSwitchAdmin`` counterpart (untouched — a switch
        carries no Dante name of its own) does not need:

        - A device carrying a unit ID whose *computed* name would exceed
          Dante's 31-character limit is **skipped**, not saved — this
          action calls ``current.save()`` directly, never
          ``full_clean()``, so decision 2's enforcement is otherwise
          bypassed and could write a name the device's own change form
          would then refuse. Reuses the existing ``_HostnameRecomputeResult
          ("skipped", reason, …)`` shape rather than a bespoke branch, so
          it reports through the same ``skipped`` bucket as "missing
          owner and type's hostname_slug".
        - A renamed, unit-ID-carrying device emits the Dante rename
          warning; a null-unit-ID device whose (possibly unchanged)
          hostname is over 31 characters emits the advisory — both via
          the same helpers the change form's ``_post_clean()`` uses, so
          wording can't drift between the two paths.
        """
        renamed = 0
        unchanged = 0
        skipped: list[str] = []
        # Built once, outside the per-device lock loop below (review
        # council finding 4) — reading current.device_type.device_model.
        # hostname_slug fresh on every iteration would cost two extra
        # queries per row while that row's SELECT ... FOR UPDATE is held
        # by _lock_devices_by_pk(), since that helper deliberately carries
        # no select_related (widening it would make MariaDB also lock the
        # joined device_type/device_model rows, and the helper is shared
        # with the fit-card and delete paths, which must not pay that
        # cost). An ordinary, non-locking read against the whole queryset
        # up front avoids both problems: no extra query inside the lock,
        # and no join on the locked row itself.
        slug_by_device_type_id = dict(
            NetworkDeviceType.objects.filter(
                pk__in=queryset.values_list("device_type_id", flat=True)
            ).values_list("pk", "device_model__hostname_slug")
        )
        for device in queryset:
            with transaction.atomic():
                locked = _lock_devices_by_pk(device.pk)
                current = locked.get(device.pk)
                if current is None:
                    continue
                original_hostname = current.hostname
                result = _recompute_hostname(
                    current,
                    # ADR 0026 PR 2 — hostname_slug lives on device_model
                    # now; looked up from the map built above, not by
                    # dereferencing current.device_type.device_model.
                    type_slug=slug_by_device_type_id[current.device_type_id],
                    exclude_switch_pk=None,
                    exclude_device_pk=current.pk,
                )
                # Decision 7 — only reachable once a name was actually
                # computed (result.status != "skipped" already covers a
                # missing owner/type-slug); never overrides that skip's
                # own reason.
                #
                # over_length_skip tracks *which* skip this is, because the
                # two must not save() the same way (Codex review finding
                # 1). The pre-existing blocked-stem skip (missing owner or
                # type's hostname_slug) never touches current.hostname, so
                # phase 18's own current.save() below still needs to run —
                # it is the only place a rack-derived owner
                # (_recompute_hostname() step 1, "stored regardless of
                # what happens next") gets persisted for a row that was
                # never touched by the add form. The over-length skip is
                # different in kind: current.hostname now holds a
                # too-long value full_clean() would reject, so save()
                # must be skipped entirely or this would strand the row
                # in a state its own change form then refuses.
                over_length_skip = False
                if result.status != "skipped" and current.dante_unit_id is not None:
                    assembled_length = dante.DANTE_UNIT_ID_PREFIX_LENGTH + len(current.hostname)
                    if assembled_length > dante.DANTE_NAME_MAX_LENGTH:
                        result = _HostnameRecomputeResult(
                            "skipped",
                            f"computed hostname {current.hostname!r} ({len(current.hostname)} "
                            f"characters) would assemble to {assembled_length} with Dante unit ID "
                            f"{current.dante_unit_id}, over the {dante.DANTE_NAME_MAX_LENGTH}-"
                            "character Dante limit",
                            result.advisories,
                        )
                        over_length_skip = True
                if result.status == "skipped":
                    skipped.append(f"{current} ({result.reason})")
                    if over_length_skip:
                        continue
                    current.save()  # phase 18: still persists a rack-derived owner (see above)
                    continue
                current.save()
                if result.status == "renamed":
                    renamed += 1
                    # Decision 8 — fires whenever this rename changes a
                    # unit-ID device's Dante name; the recompute action
                    # never touches dante_unit_id itself, so old and new
                    # are the same value here. Also requires an actual
                    # previous Dante name to lose (``old_name is not
                    # None``, the same guard NetworkDeviceChangeForm.
                    # _post_clean() applies) — a unit-ID device that never
                    # had a hostname before this run had nothing for
                    # Dante to route by, so there is nothing to
                    # re-subscribe.
                    if current.dante_unit_id is not None:
                        old_name = dante.dante_device_name(current.dante_unit_id, original_hostname)
                        if old_name is not None:
                            # Labelled by the *old* hostname (ADR 0024's
                            # own pinned example — see the identical
                            # comment on NetworkDeviceChangeForm.
                            # _post_clean()): Dante Controller still shows
                            # the pre-rename name until the operator acts
                            # on this warning.
                            old_label = original_hostname or f"Device #{current.pk}"
                            messages.warning(
                                request,
                                dante.rename_warning(
                                    old_label,
                                    old_unit_id=current.dante_unit_id,
                                    new_unit_id=current.dante_unit_id,
                                    old_name=old_name,
                                    new_name=current.dante_device_name,
                                ),
                            )
                else:
                    unchanged += 1
                # Decision 6 — NetworkDevice-only, unit-ID-null only, and
                # unconditional on renamed-vs-unchanged: an already-over-
                # length hostname that recompute leaves untouched is still
                # worth flagging, same as the model's own blocking check
                # doesn't care whether a save is a rename.
                if current.dante_unit_id is None:
                    advisory = dante.over_length_advisory(current.hostname)
                    if advisory is not None:
                        messages.info(request, advisory)
                for advisory in result.advisories:
                    messages.info(request, advisory)
        if renamed:
            messages.success(request, f"Recomputed {renamed} hostname(s).")
        if unchanged:
            messages.info(request, f"{unchanged} hostname(s) already up to date.")
        if skipped:
            messages.warning(request, f"Skipped {len(skipped)}: {'; '.join(skipped)}.")

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
            create_form = NetworkDeviceAddForm(instance=NetworkDevice(host=host))
        create_form.fields["device_type"].queryset = NetworkDeviceType.objects.filter(  # type: ignore[attr-defined]
            is_add_in_card=True
        ).select_related("device_model")
        # device_type__device_model (Codex review of ADR 0026's PR 1) — the
        # template renders card.device_type per row, which now
        # dereferences device_model; a bare "device_type" here left one
        # extra query per hostless card.
        existing_cards = (
            NetworkDevice.objects.filter(device_type__is_add_in_card=True, host__isnull=True)
            .exclude(pk=host.pk)
            .select_related("device_type__device_model")
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
        with transaction.atomic():
            locked = _lock_devices_by_pk(host.pk)
            locked_host = locked.get(host.pk)
            if locked_host is None:
                messages.error(request, "That host no longer exists.")
                return redirect("admin:inventory_networkdevice_changelist")
            if locked_host.device_type.is_add_in_card:
                messages.error(request, f"{locked_host} is itself an add-in card and cannot host another.")
                return redirect("admin:inventory_networkdevice_change", locked_host.pk)
            # NetworkDeviceAddForm (review note 5) — a bespoke form here
            # would silently bypass that form's rack-slot suggester and
            # materialization pre-flight. instance=NetworkDevice(host=
            # locked_host) so full_clean() sees the relationship and the
            # row is inserted once with host already set, never patched in
            # afterward.
            form = NetworkDeviceAddForm(data=request.POST, instance=NetworkDevice(host=locked_host))
            form.fields["device_type"].queryset = NetworkDeviceType.objects.filter(  # type: ignore[attr-defined]
                is_add_in_card=True
            ).select_related("device_model")
            if not form.is_valid():
                return self._render_fit_card_page(request, locked_host, create_form=form)
            form.instance.created_by = request.user
            new_card = form.save()
        messages.success(request, f"{new_card} created and fitted to {host}.")
        return redirect("admin:inventory_networkdevice_change", host.pk)
