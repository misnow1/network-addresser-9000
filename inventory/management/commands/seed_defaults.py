"""Seed the system Default VLAN and Default Switch Port VLAN Profile.

Migration ``0006_switch_port_vlan_profiles`` seeds these rows too, but only
once, at the moment it applies — it can't repair them if they're later
removed by ``manage.py flush`` (which truncates every table, migration
history included in spirit, since the seeded rows are ordinary data rows)
or otherwise deleted outside the model layer's own delete-blocking guards.
This command re-seeds them any time, against the app's *real* models
(unlike the migration, which must use its historical ``apps.get_model()``
snapshot) — see ``inventory.models.default_switch_port_vlan_profile()``,
which every unselected ``NetworkSwitchTypePort`` falls back to and which
raises if this row is ever missing.

Run after every ``migrate`` (idempotent, safe to re-run) — same role as
``sync_roles``, see that command for why role-sync can't be a data
migration either. See CONTEXT.md and ADR 0012 for what these rows mean.
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from inventory.models import (
    DEFAULT_PROFILE_NAME,
    DEFAULT_VLAN_ID,
    DEFAULT_VLAN_NAME,
    VLAN,
    SwitchPortVlanProfile,
)


class Command(BaseCommand):
    help = "Seed the system Default VLAN and Default Switch Port VLAN Profile (see ADR 0012)."

    def handle(self, *args: Any, **options: Any) -> None:
        with transaction.atomic():
            vlan, vlan_created = VLAN.objects.get_or_create(
                vlan_id=DEFAULT_VLAN_ID,
                defaults={"name": DEFAULT_VLAN_NAME, "subnet": ""},
            )
            # Whether VLAN id=1 diverges from the seed's expectations. Once
            # the system profile exists, this is just informational — VLAN 1
            # is an ordinary, editable VLAN like any other, and a later
            # rename/re-addressing of it is a legitimate administrative
            # action, not a conflict (unlike the profile's `is_system`
            # fields, nothing about VLAN 1 is documented as permanently
            # locked). It only becomes dangerous below, when a *new* system
            # profile is about to be created and would otherwise be silently
            # wired to whatever this mismatched row happens to be.
            vlan_mismatched = not vlan_created and (vlan.name != DEFAULT_VLAN_NAME or vlan.subnet != "")

            # Keyed on ``is_system``, NOT on ``name``: the system profile's
            # name is deliberately editable (ADR 0012 — it's a cosmetic
            # label), and this command runs on every container start via
            # docker/entrypoint.sh. Keying on the name would mean a single
            # supported rename causes the next restart to seed a *second*
            # is_system row, after which ``default_switch_port_vlan_profile()``
            # raises MultipleObjectsReturned on every switch-port creation —
            # and neither row can be deleted through the app to recover.
            profile = SwitchPortVlanProfile.objects.filter(is_system=True).first()
            profile_created = profile is None
            if profile is None:
                if vlan_mismatched:
                    raise CommandError(
                        f"No system profile exists yet, but VLAN id={DEFAULT_VLAN_ID} already exists "
                        f"as {vlan.name!r} (subnet={vlan.subnet!r}), not the expected "
                        f"{DEFAULT_VLAN_NAME!r} with no subnet. Seeding the system profile against "
                        "this VLAN would silently wire it to the wrong row — resolve the conflict "
                        "before re-running."
                    )
                if SwitchPortVlanProfile.objects.filter(name=DEFAULT_PROFILE_NAME).exists():
                    # ``name`` is unique, so creating would raise IntegrityError
                    # and (under entrypoint.sh's ``set -e``) wedge startup with
                    # an opaque error. Fail with an actionable one instead.
                    raise CommandError(
                        f"No system profile exists, but an ordinary profile is already named "
                        f"{DEFAULT_PROFILE_NAME!r}. Rename or remove it, then re-run — the system "
                        "profile cannot be seeded while its default name is taken."
                    )
                SwitchPortVlanProfile.objects.create(
                    name=DEFAULT_PROFILE_NAME,
                    port_mode="trunk",
                    native_vlan=vlan,
                    all_vlans_allowed=True,
                    is_system=True,
                )
            else:
                if vlan_mismatched:
                    self.stdout.write(
                        f"VLAN id={DEFAULT_VLAN_ID} present as {vlan.name!r} (subnet={vlan.subnet!r}), "
                        f"not the originally-seeded {DEFAULT_VLAN_NAME!r} with no subnet — leaving it "
                        "as-is; the system profile already references this VLAN."
                    )
                if profile.name != DEFAULT_PROFILE_NAME:
                    self.stdout.write(
                        f"System profile present as {profile.name!r} (renamed from "
                        f"{DEFAULT_PROFILE_NAME!r}) — leaving it as-is."
                    )

        self.stdout.write(
            self.style.SUCCESS(
                f"Default VLAN: {'created' if vlan_created else 'already present'}. "
                f"Default profile: {'created' if profile_created else 'already present'}."
            )
        )
