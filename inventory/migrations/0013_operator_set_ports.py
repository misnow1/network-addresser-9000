# Generated for ADR 0022 PR 1 (operator-set ports and port labels).

import inventory.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0012_vlan_department'),
    ]

    operations = [
        migrations.AddField(
            model_name='networkdevicetypeport',
            name='address_source',
            field=models.CharField(choices=[('slot', "From the device's rack slot"), ('operator', 'Set by the operator')], default='slot', help_text="Where this port's address comes from. Leave at the default unless this port is a second address on a VLAN this device already uses — a Yamaha console's Device Control interface — which the system cannot compute and the operator must supply.", max_length=10),
        ),
        migrations.AddField(
            model_name='networkdevicetypeport',
            name='hostname_suffix',
            field=models.CharField(blank=True, help_text='Names this port in address lists as "<device hostname>-<suffix>", e.g. "engine" for a console\'s audio engine. Store it bare, with no leading dash. Leave blank for a port that shares the device\'s own name.', max_length=63, validators=[inventory.validators.validate_dns_label]),
        ),
    ]
