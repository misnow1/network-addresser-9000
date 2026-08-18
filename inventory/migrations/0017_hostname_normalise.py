# ADR 0023 decision 8 (amended twice) / PLAN-hostname-computation.md PR 1.
#
# Shrinks NetworkSwitch.hostname/NetworkDevice.hostname from CharField(255) to
# CharField(63) (the 63-cap decision 8 settles) and backfills every existing,
# non-blank hostname to stripped-and-lowercased (the amendment: on-write
# normalisation alone can never touch a row nobody writes to again). 40 of 83
# live rows change.
#
# Three RunPython/AlterField steps, in this order and not fewer:
#   1. The casing backfill itself — deliberately run *first*, against the
#      still-wide CharField(255) column. Safe there: it only ever shortens a
#      value (strip removes characters, lower() never adds any), so it can
#      never make anything longer while the column is still wide enough to
#      hold whatever it started as.
#   2. A pre-check that raises, naming every offending row, if any hostname
#      is still longer than 63 characters *after* the backfill above — before
#      the AlterField, so a length violation refuses cleanly instead of
#      MySQL truncating (or erring mid-ALTER with a row number and no
#      context) after the schema has already changed. None are longer today
#      (longest live value is 22).
#   3. AlterField x2 for max_length.
#
# Code review (round 2, finding 1) — an earlier revision ran the pre-check
# *before* the backfill and measured len(hostname.strip()) there, reasoning
# that the backfill would shorten a whitespace-padded value to fit. That
# reasoning was backwards: the pre-check ran before the backfill had touched
# anything, so a raw value like "  " + "a"*63 + "  " (67 characters, 63 once
# stripped) passed the pre-check on its stripped length, then reached the
# AlterField still 67 characters wide — MariaDB's STRICT_TRANS_TABLES raises
# 1406 mid-ALTER, exactly the opaque failure the pre-check exists to
# prevent. Running the backfill first closes this: by the time the
# pre-check runs, every non-blank hostname is already stripped and
# lowercased, so len(hostname.strip()) and len(hostname) agree — the
# .strip() stays anyway, as a harmless, defensive measurement rather than a
# load-bearing one.
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


def _backfill_hostname_casing(apps, schema_editor):
    """Strip and lowercase every non-blank hostname on both models — 40 of
    83 live rows change. Runs *first*, against the still-wide
    CharField(255) column (see the module header) — safe there because it
    only ever shortens a value. Safe in general too: no two hostnames
    collide once lowercased, confirmed against the live database while
    planning this phase.
    """
    NetworkSwitch = apps.get_model("inventory", "NetworkSwitch")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    alias = schema_editor.connection.alias
    for model in (NetworkSwitch, NetworkDevice):
        for pk, hostname in model.objects.using(alias).filter(hostname__gt="").values_list("pk", "hostname"):
            normalised = hostname.strip().lower()
            if normalised != hostname:
                model.objects.using(alias).filter(pk=pk).update(hostname=normalised)


def _check_hostname_lengths(apps, schema_editor):
    """Refuse rather than truncate if any stored hostname is still longer
    than the incoming 63-character cap *after* the backfill above has
    already run — before the AlterField, since a failure raised *after*
    the ALTER TABLE would leave the schema changed with no way back
    (MySQL DDL isn't transactional). ``.strip()`` here is defensive, not
    load-bearing, now that the backfill has already stripped everything
    it can reach — see the module header for why an earlier revision that
    measured the stripped length *before* backfilling was unsafe.
    """
    NetworkSwitch = apps.get_model("inventory", "NetworkSwitch")
    NetworkDevice = apps.get_model("inventory", "NetworkDevice")
    alias = schema_editor.connection.alias
    overlong = []
    for model in (NetworkSwitch, NetworkDevice):
        for pk, hostname in model.objects.using(alias).filter(hostname__gt="").values_list("pk", "hostname"):
            stripped_length = len(hostname.strip())
            if stripped_length > 63:
                overlong.append(f"{model.__name__} pk={pk} ({stripped_length} chars): {hostname!r}")
    if overlong:
        raise RuntimeError(
            "Cannot shrink hostname to max_length=63 — the following row(s) already exceed it:\n"
            + "\n".join(overlong)
        )


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0016_hostname_ingredients"),
    ]

    operations = [
        migrations.RunPython(
            _backfill_hostname_casing,
            # Irreversible in practice — the original casing is
            # unrecoverable once overwritten, so reversing is a no-op
            # rather than a guessed re-uppercase (ADR 0023 decision 8's
            # amendment).
            reverse_code=migrations.RunPython.noop,
        ),
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
    ]
