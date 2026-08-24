"""ADR 0026 / PLAN-adr-0026.md settled decision 6 — schema half of the
device-model extraction, split from ``0021_device_model_backfill``'s data
half for readability and reversibility (see that migration's module
docstring for why).

Creates ``NetworkDeviceModel`` and adds ``NetworkDeviceType.device_model``
as **nullable** — no constraint changes here. Nothing reads or writes the
new column yet; ``0021`` backfills it, then tightens it to ``NOT NULL`` and
replaces ``unique_device_type``.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0019_dante_unit_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="NetworkDeviceModel",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("manufacturer", models.CharField(max_length=100)),
                ("model", models.CharField(max_length=100)),
                (
                    "description",
                    models.CharField(
                        blank=True,
                        help_text=(
                            'What this hardware is, e.g. "Dante Interface with AES3 I/O" — a '
                            "model-level fact, not a profile's port purpose (see "
                            "NetworkDeviceTypePort.description). Blank is fine; nothing depends "
                            "on it being filled."
                        ),
                        max_length=255,
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        editable=False,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["manufacturer", "model"],
            },
        ),
        migrations.AddField(
            model_name="networkdevicetype",
            name="device_model",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="profiles",
                to="inventory.networkdevicemodel",
            ),
        ),
        migrations.AddConstraint(
            model_name="networkdevicemodel",
            constraint=models.UniqueConstraint(fields=("manufacturer", "model"), name="unique_device_model"),
        ),
        migrations.AddConstraint(
            model_name="networkdevicemodel",
            constraint=models.CheckConstraint(
                condition=models.Q(("manufacturer", ""), _negated=True),
                name="networkdevicemodel_manufacturer_not_blank",
            ),
        ),
        migrations.AddConstraint(
            model_name="networkdevicemodel",
            constraint=models.CheckConstraint(
                condition=models.Q(("model", ""), _negated=True), name="networkdevicemodel_model_not_blank"
            ),
        ),
    ]
