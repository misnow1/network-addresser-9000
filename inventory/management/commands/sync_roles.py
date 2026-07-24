"""Create/update the Viewer, Editor, and Admin RBAC groups.

Reuses Django's own ``Group``/``Permission`` system rather than a custom
role field — every ``ModelAdmin``'s default permission checks already
consult ``request.user.has_perm(...)`` against these standard codenames, so
assigning a user to one of these groups (and setting ``is_staff=True``,
required for any Django-admin access) is all "role assignment" takes.

Permission rows for this app's own models don't exist yet at
migration-application time (they're created by the ``post_migrate`` signal
*after* the whole ``migrate`` run finishes), so this can't be a data
migration — run it explicitly after ``migrate`` instead. Idempotent: safe
to re-run any time, e.g. after adding a model.

See CONTEXT.md's "Roles" section for the canonical definition of what each
role can do.
"""

from typing import Any

from django.apps import apps
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create/update the Viewer, Editor, and Admin RBAC groups (see CONTEXT.md's Roles section)."

    def handle(self, *args: Any, **options: Any) -> None:
        inventory_models = apps.get_app_config("inventory").get_models()
        content_types = list(ContentType.objects.get_for_models(*inventory_models).values())

        inventory_perms = Permission.objects.filter(content_type__in=content_types)
        view_perms = list(inventory_perms.filter(codename__startswith="view_"))
        add_perms = list(inventory_perms.filter(codename__startswith="add_"))
        change_perms = list(inventory_perms.filter(codename__startswith="change_"))
        delete_perms = list(inventory_perms.filter(codename__startswith="delete_"))

        # Viewers can also see the audit trail itself — CONTEXT.md's Viewer
        # role is "can see all data," and who-changed-what is part of that.
        log_entry_view_perm = Permission.objects.filter(
            content_type__app_label="auditlog", codename="view_logentry"
        )
        view_perms += list(log_entry_view_perm)

        viewer, _ = Group.objects.get_or_create(name="Viewer")
        viewer.permissions.set(view_perms)

        editor, _ = Group.objects.get_or_create(name="Editor")
        editor.permissions.set(view_perms + add_perms + change_perms)

        admin, _ = Group.objects.get_or_create(name="Admin")
        admin.permissions.set(view_perms + add_perms + change_perms + delete_perms)

        self.stdout.write(
            self.style.SUCCESS(
                f"Viewer: {viewer.permissions.count()} perms, "
                f"Editor: {editor.permissions.count()} perms, "
                f"Admin: {admin.permissions.count()} perms."
            )
        )
