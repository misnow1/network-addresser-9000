# Generated for ADR 0022 PR 3 (add-in cards).
#
# 0014 dropped a OneToOneField named ``host`` (the ADR 0018 companion
# relationship); this adds a plain ForeignKey of the same name (the ADR 0022
# "currently fitted to" pointer) — two unrelated relationships that happen to
# share a name across two migrations. No data migration: the production
# database is rebuilt from the CSVs (ADR 0022, "What this does to production
# data"), and every existing row gets ``host=NULL``/``is_add_in_card=False``,
# reproducing today's behaviour exactly.
#
# No database CheckConstraint for the self-host edge, despite the plan that
# scoped this PR calling for one (``~Q(host=F("id"))``) — MariaDB refuses any
# CHECK constraint that references an AUTO_INCREMENT column outright (error
# 1901, "Function or expression 'AUTO_INCREMENT' cannot be used in the CHECK
# clause of `id`"), verified directly against this project's MariaDB with a
# bare ``CHECK (id > 0)`` on an unrelated table. All three of decision 6's
# edges are therefore enforced only in ``NetworkDevice._check_host_
# invariants()`` (``models.py``), called from both ``save()`` and ``clean()``.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0014_retire_companions"),
    ]

    operations = [
        migrations.AddField(
            model_name="networkdevice",
            name="host",
            field=models.ForeignKey(
                blank=True,
                help_text="The device this add-in card is currently fitted inside, if any (ADR 0022). A soft 'currently fitted to' pointer with no addressing meaning whatsoever — it does not derive an address, constrain a VLAN, share an ordinal, or move anything. A pulled card keeps its own rack slot and addresses. Only meaningful when this device's own type is an add-in card; fitting happens through the dedicated 'Fit a card' flow, never this field directly.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="installed_cards",
                to="inventory.networkdevice",
            ),
        ),
        migrations.AddField(
            model_name="networkdevicetype",
            name="is_add_in_card",
            field=models.BooleanField(
                default=False,
                help_text="This type's instances are cards fitted inside another device and routinely moved between hosts — a DMI-DANTE, an X-Dante. Leave off for ordinary equipment.",
            ),
        ),
    ]
