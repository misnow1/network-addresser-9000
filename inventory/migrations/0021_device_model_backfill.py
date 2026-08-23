"""ADR 0026 / PLAN-adr-0026.md settled decision 6 — data half of the
device-model extraction. Split from ``0020_networkdevicemodel``'s schema
half for readability and independent reversibility, **not** because a
``RunPython`` in the same migration as the field it reads is unsafe —
``0011_device_companions.py`` already adds ``companion_type`` and reads it
from a ``RunPython`` in one migration, so operations within a migration run
sequentially and that would have worked too. Both migrations ship together
in PR 1.

Deterministic and needs no re-import: every ``(manufacturer, model)`` pair
already in the database collapses to exactly one ``NetworkDeviceModel``
row, every existing ``NetworkDeviceType`` is re-pointed at it, and
``description`` is blank for all of them — new metadata nothing here can
infer.

Seven operations, in this exact order — **do not reorder them**:

1. ``RunPython(collapse_and_repoint, reverse_code=noop)`` — the forward
   collapse.
2. ``AlterField`` ``device_model`` -> ``null=False``.
3. ``RemoveConstraint("unique_device_type")`` — must come *before* the
   ``RemoveField``s below, or dropping ``manufacturer``/``model`` under a
   live constraint that still names them fails on MariaDB (the same
   ordering ``0004_...`` already uses for this exact constraint).
4. ``AlterModelOptions`` — ``ordering`` off ``manufacturer``/``model``.
5. ``RunPython(noop, reverse_code=repopulate_strings)`` — a forward no-op
   that exists *only* so the reverse direction has somewhere to run. See
   "The reverse is not free" below.
6. ``RemoveField`` ``manufacturer``, ``RemoveField`` ``model``.
7. ``AddConstraint`` — ``unique_device_type`` on ``["device_model", "name"]``,
   same name as before (no gain in renaming it, and nothing asserts on the
   constraint's name).

**The reverse is not free, and it is lossy.** Reversing runs 7 -> 1. Step 6
reversed re-adds ``manufacturer``/``model`` as ``NOT NULL`` columns with
Django's empty-string effective default, so every row briefly holds
``("", "")``. If step 3's old ``unique_device_type`` (on
``(manufacturer, model, name)``) were restored at that point, it would
collide immediately — the estate has 22 profiles named "Default", all
blank on both columns. Step 5's ``reverse_code``
(``repopulate_strings``) is what prevents that: sitting between steps 3
and 6, it runs *after* the columns exist (6 already reversed) and *before*
the old constraint returns (3 not yet reversed), repopulating
``manufacturer``/``model`` from each type's ``device_model`` FK so the old
constraint sees real, distinct values again.

That repopulation cannot recover ``description`` — the text has no column
to go back to on ``NetworkDeviceType``, so reversing this migration
**destroys every description**. This is a deliberate, accepted loss
(PLAN-adr-0026.md), not an oversight; no attempt is made to preserve it.
"""

from django.db import migrations, models


def collapse_and_repoint(apps, schema_editor):
    """Collapse every distinct ``(manufacturer, model)`` pair already on
    ``NetworkDeviceType`` into one ``NetworkDeviceModel`` row, then
    re-point every type at it.

    ``.order_by()`` on the ``values_list().distinct()`` query below is
    **load-bearing, not stylistic** — do not remove it. ``NetworkDeviceType.
    Meta.ordering`` is still ``["manufacturer", "model", "name"]`` at this
    point in the migration graph (the ``AlterModelOptions`` that changes it
    is step 4, below), and a historical model built by ``apps.get_model()``
    carries that ``Meta.ordering`` exactly like the live model would.
    Django appends ``ORDER BY`` columns from ``Meta.ordering`` to a
    ``SELECT DISTINCT`` that doesn't declare its own ordering, so without
    ``.order_by()`` here the query actually issued is
    ``SELECT DISTINCT manufacturer, model, name ... ORDER BY 1, 2, name`` —
    ``name`` leaks into the distinctness itself. Martin Audio IK-42 is real
    production data carrying two profile names ("with Dante Card",
    "without Dante Card") for one model, so that leaked column would
    produce *two* IK-42 rows here, and the second ``create()`` below would
    violate ``unique_device_model``. Measured directly against this tree on
    Django 6.0.7 (``.query`` on each queryset, no ``.order_by()`` vs. with):

        -- without .order_by()  (note the third column)
        SELECT DISTINCT `manufacturer`, `model`, `name`
            FROM `inventory_networkdevicetype` ORDER BY 1 ASC, 2 ASC, `name` ASC
        -- with .order_by()
        SELECT DISTINCT `manufacturer`, `model` FROM `inventory_networkdevicetype`

    ``created_by`` is left null on the created rows (the field is
    ``null=True``, ``SET_NULL``), matching every other data migration here.
    """
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    NetworkDeviceModel = apps.get_model("inventory", "NetworkDeviceModel")

    for manufacturer, model in (
        NetworkDeviceType.objects.order_by()  # <-- load-bearing, see docstring above
        .values_list("manufacturer", "model")
        .distinct()
    ):
        device_model = NetworkDeviceModel.objects.create(
            manufacturer=manufacturer, model=model, description=""
        )
        NetworkDeviceType.objects.filter(manufacturer=manufacturer, model=model).update(
            device_model=device_model
        )


def repopulate_strings(apps, schema_editor):
    """Reverse of the forward no-op at step 5 — see the module docstring's
    "The reverse is not free" section for exactly where in the reverse
    sequence this runs and why.

    **Lossy.** Every ``NetworkDeviceModel.description`` is discarded here —
    there is no column on ``NetworkDeviceType`` for it to go back to. That
    is an accepted consequence of reversing this migration, not a bug in
    this function.
    """
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    for device_type in NetworkDeviceType.objects.select_related("device_model"):
        device_type.manufacturer = device_type.device_model.manufacturer
        device_type.model = device_type.device_model.model
        device_type.save(update_fields=["manufacturer", "model"])


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0020_networkdevicemodel"),
    ]

    operations = [
        migrations.RunPython(collapse_and_repoint, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name="networkdevicetype",
            name="device_model",
            field=models.ForeignKey(
                on_delete=models.PROTECT, related_name="profiles", to="inventory.networkdevicemodel"
            ),
        ),
        migrations.RemoveConstraint(
            model_name="networkdevicetype",
            name="unique_device_type",
        ),
        migrations.AlterModelOptions(
            name="networkdevicetype",
            options={"ordering": ["device_model__manufacturer", "device_model__model", "name"]},
        ),
        migrations.RunPython(migrations.RunPython.noop, reverse_code=repopulate_strings),
        migrations.RemoveField(
            model_name="networkdevicetype",
            name="manufacturer",
        ),
        migrations.RemoveField(
            model_name="networkdevicetype",
            name="model",
        ),
        migrations.AddConstraint(
            model_name="networkdevicetype",
            constraint=models.UniqueConstraint(fields=("device_model", "name"), name="unique_device_type"),
        ),
    ]
