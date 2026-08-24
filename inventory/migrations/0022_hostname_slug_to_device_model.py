"""ADR 0026 / PLAN-adr-0026.md PR 2 — moves ``hostname_slug`` off
``NetworkDeviceType`` onto ``NetworkDeviceModel``, the same class of
model-not-profile fact as ``description`` (PR 1), but *blocking* (ADR 0023
decision 1): a divergent slug between two profiles of one model used to
silently compute different hostnames for identical hardware, and after this
migration there is only one value to read.

Four operations, in this exact order — **do not reorder them**:

1. ``RunPython(assert_profiles_agree, reverse_code=noop)`` — preflight,
   **before any DDL**. Production is MariaDB (``config/settings.py:118-
   121``), whose DDL is not transactional: an assertion raised *after*
   ``AddField`` would leave the new column behind on a migration Django
   never records as applied, and a retry would then hit a partially
   applied, unrecorded migration. Asserting first, against the schema as
   of ``0021_device_model_backfill``, means a failure here leaves nothing
   behind to clean up.
2. ``AddField`` ``hostname_slug`` on ``NetworkDeviceModel``.
3. ``RunPython(copy_slugs, reverse_code=copy_back)`` — copy each model's
   now-verified-to-agree slug up from its profiles.
4. ``RemoveField`` ``hostname_slug`` from ``NetworkDeviceType``.

**Clean-retry is only proven for the preflight-abort path (step 1).** A
failure in step 3 or 4 instead — after ``AddField`` has already run and
committed, since MariaDB DDL isn't transactional — leaves that column in
place while this migration is still unrecorded as applied; a rerun would
then hit ``AddField`` again and fail with ``Duplicate column name``, not
recover cleanly the way an aborted preflight does.

**The reverse is simpler than 0021's, and needs no placeholder operation.**
Reversing runs 4 -> 1. Step 4 reversed (``AddField``) restores the Type
column as ``NOT NULL`` filled with Django's empty-string effective default
— valid, since ``blank=True`` and there is no slug constraint, index,
ordering, or database validator that column participates in (unlike
``0021``'s ``unique_device_type``, which is exactly why that migration
needed a placeholder step between the constraint and the column). Step 3's
``reverse_code`` then repopulates every profile's column from its model
— it *must* do this, or rollback would silently leave every profile
holding a blank slug. Step 1 reverses last and stays a no-op in both
directions; step 2 reversed (``RemoveField``) drops the column from
``NetworkDeviceModel`` last of all.

``.order_by()`` (or, here, a plain Python ``set()``) is load-bearing for
the same reason ``0021``'s backfill needed it: ``NetworkDeviceType.Meta.
ordering`` is ``["device_model__manufacturer", "device_model__model",
"name"]`` at this point in the migration graph (unchanged since ``0021``),
so a ``.values_list("hostname_slug", flat=True).distinct()`` query with no
explicit ordering would leak the ordering columns into ``SELECT DISTINCT``
and report IK-42's two identical ``"ik42"`` profiles as two distinct
values — a false disagreement that would abort a perfectly good migration.
Collecting the values into a Python ``set()`` (rather than adding
``.order_by()`` to a ``.distinct()`` query) sidesteps the issue entirely,
since the leaking only happens when the SQL-level ``DISTINCT`` combines
with default ordering; a plain (non-distinct) ``values_list()`` is
unaffected either way.
"""

from django.db import migrations, models

import inventory.validators


def _slug_sets_by_model(NetworkDeviceType):
    """``{device_model_id: {slug, ...}}`` for every model with at least one
    profile — shared by the preflight assertion and the copy step so they
    agree on exactly what "the profiles of this model" means.
    """
    slugs_by_model: dict[int, set[str]] = {}
    for device_model_id, hostname_slug in NetworkDeviceType.objects.values_list(
        "device_model_id", "hostname_slug"
    ):
        slugs_by_model.setdefault(device_model_id, set()).add(hostname_slug)
    return slugs_by_model


def assert_profiles_agree(apps, schema_editor):
    """Preflight — **before any DDL** (see module docstring). Every
    ``NetworkDeviceModel`` with two or more profiles must have every
    profile carrying the identical ``hostname_slug``; a model is not
    required to have any profiles at all (settled decision C — an orphan
    model is not a disagreement, it just has no slug to inherit and stays
    blank in ``copy_slugs`` below).
    """
    NetworkDeviceModel = apps.get_model("inventory", "NetworkDeviceModel")
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    slugs_by_model = _slug_sets_by_model(NetworkDeviceType)
    disagreements = [
        (device_model_id, slugs) for device_model_id, slugs in slugs_by_model.items() if len(slugs) > 1
    ]
    if not disagreements:
        return
    device_models = NetworkDeviceModel.objects.in_bulk([pk for pk, _slugs in disagreements])
    detail = "; ".join(
        f"{device_models[pk].manufacturer} {device_models[pk].model} (pk={pk}): {sorted(slugs)}"
        for pk, slugs in disagreements
    )
    raise RuntimeError(
        "0022_hostname_slug_to_device_model: hostname_slug disagrees across profiles of the "
        f"following device model(s) — ADR 0026 PR 2 requires every profile of one model to "
        f"already agree before the field can move: {detail}"
    )


def copy_slugs(apps, schema_editor):
    """Copy each model's now-verified-to-agree slug up from its profiles.
    An orphan model (no profiles at all) is left at its field default —
    ``""``, from ``AddField`` — which is correct, not an omission (settled
    decision C).
    """
    NetworkDeviceModel = apps.get_model("inventory", "NetworkDeviceModel")
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    slugs_by_model = _slug_sets_by_model(NetworkDeviceType)
    for device_model_id, slugs in slugs_by_model.items():
        (slug,) = slugs  # assert_profiles_agree already proved len(slugs) == 1
        NetworkDeviceModel.objects.filter(pk=device_model_id).update(hostname_slug=slug)


def copy_back(apps, schema_editor):
    """Reverse of ``copy_slugs`` — repopulates every profile's column from
    its model. Required: reverse ``RemoveField`` (step 4, reversed first)
    restores the Type column filled with blanks, and without this every
    profile would silently lose its slug on rollback.
    """
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    for device_type in NetworkDeviceType.objects.select_related("device_model"):
        device_type.hostname_slug = device_type.device_model.hostname_slug
        device_type.save(update_fields=["hostname_slug"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0021_device_model_backfill"),
    ]

    operations = [
        migrations.RunPython(assert_profiles_agree, reverse_code=migrations.RunPython.noop),
        migrations.AddField(
            model_name="networkdevicemodel",
            name="hostname_slug",
            field=models.CharField(
                blank=True,
                help_text=(
                    'Operator-set hostname abbreviation, e.g. "ik42". Never auto-filled — '
                    'slugify("IK-42") gives "ik-42" where the name in use might be "ik42". '
                    "Blank means no profile of this model offers a computed hostname (ADR 0023)."
                ),
                max_length=63,
                validators=[inventory.validators.validate_dns_label],
            ),
        ),
        migrations.RunPython(copy_slugs, reverse_code=copy_back),
        migrations.RemoveField(
            model_name="networkdevicetype",
            name="hostname_slug",
        ),
    ]
