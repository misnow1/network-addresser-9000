# ADR 0023 decision 8 (amended twice) / PLAN-hostname-computation.md PR 1.
#
# Shrinks NetworkSwitch.hostname/NetworkDevice.hostname from CharField(255) to
# CharField(63) (the 63-cap decision 8 settles) and backfills every existing,
# non-blank hostname to stripped-and-lowercased (the amendment: on-write
# normalisation alone can never touch a row nobody writes to again). 40 of 83
# live rows change.
#
# Three RunPython/AlterField steps, in this order and not fewer, because
# MySQL cannot roll back DDL:
#   1. A pre-check that raises, naming every offending row, if any hostname
#      is already longer than 63 characters — before the AlterField, so a
#      length violation refuses cleanly instead of MySQL truncating (or
#      erring mid-ALTER with a row number and no context) after the schema
#      has already changed. None are longer today (longest live value is 22).
#   2. AlterField x2 for max_length.
#   3. The casing backfill itself.
#
# Historical models throughout (apps.get_model()), not the real ones — the
# real NetworkSwitch.save()/NetworkDevice.save() normalise casing themselves,
# and running that on every row here would be redundant at best and, since
# save() also re-materializes on is_new, actively wrong. Reverse is a no-op
# for both RunPython steps: the pre-check has nothing to reverse, and the
# original casing is unrecoverable — re-uppercasing would be a guess, not a
# rollback (settled decision, ADR 0023 decision 8's amendment).
#
# Deliberately not audited (config/settings.py's AUDITLOG_INCLUDE_TRACKING_
# MODELS) — this is a historical-model data fix run once at deploy time, not
# an operator action, the same posture 0014_retire_companions.py takes.

from django.db import migrations, models


def _check_hostname_lengths(apps, schema_editor):
    """Refuse rather than truncate if any stored hostname is already longer
    than the incoming 63-character cap — must run before the AlterField
    below, since a failure raised *after* the ALTER TABLE would leave the
    schema changed with no way back (MySQL DDL isn't transactional).
    """
    NetworkSwitch = apps.get_model("inventory", "NetworkSwitch")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    alias = schema_editor.connection.alias
    overlong = []
    for model in (NetworkSwitch, NetworkDevice):
        for pk, hostname in model.objects.using(alias).filter(hostname__gt="").values_list("pk", "hostname"):
            # .strip() before measuring (code review finding 5a) — the
            # backfill below strips too, so a value that only exceeds 63
            # characters including leading/trailing whitespace would have
            # fit fine after backfill; measuring the raw value here would
            # abort the migration over a row that was never actually a
            # problem.
            stripped_length = len(hostname.strip())
            if stripped_length > 63:
                overlong.append(f"{model.__name__} pk={pk} ({stripped_length} chars): {hostname!r}")
    if overlong:
        raise RuntimeError(
            "Cannot shrink hostname to max_length=63 — the following row(s) already exceed it:\n"
            + "\n".join(overlong)
        )


def _backfill_hostname_casing(apps, schema_editor):
    """Strip and lowercase every non-blank hostname on both models — 40 of
    83 live rows change. Safe: no two hostnames collide once lowercased,
    confirmed against the live database while planning this phase.
    """
    NetworkSwitch = apps.get_model("inventory", "NetworkSwitch")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    alias = schema_editor.connection.alias
    for model in (NetworkSwitch, NetworkDevice):
        for pk, hostname in model.objects.using(alias).filter(hostname__gt="").values_list("pk", "hostname"):
            normalised = hostname.strip().lower()
            if normalised != hostname:
                model.objects.using(alias).filter(pk=pk).update(hostname=normalised)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0016_hostname_ingredients"),
    ]

    operations = [
        migrations.RunPython(_check_hostname_lengths, reverse_code=migrations.RunPython.noop),
        migrations.AlterField(
            model_name="networkswitch",
            name="hostname",
            field=models.CharField(blank=True, max_length=63),
        ),
        migrations.AlterField(
            model_name="networkdevice",
            name="hostname",
            field=models.CharField(blank=True, max_length=63),
        ),
        migrations.RunPython(
            _backfill_hostname_casing,
            # Irreversible in practice — the original casing is
            # unrecoverable once overwritten, so reversing is a no-op
            # rather than a guessed re-uppercase (ADR 0023 decision 8's
            # amendment).
            reverse_code=migrations.RunPython.noop,
        ),
    ]
