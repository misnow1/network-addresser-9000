# ADR 0023 decision 10, amended (phase 18 PR 4) — seeds hostname_slug on
# the 8 switch Types and Neutrik NA2-DLINE that are still blank. A rebuild
# from the CSVs picks these up via import_prod_data.py's own copy of this
# constant; the database that already exists needs this migration instead,
# since it will not be rebuilt just to pick them up.
#
# Matches on (manufacturer, model), not the profile — a Type's
# hostname_slug depends only on its hardware, and this migration must give
# both IK-42 profiles (etc.) the same value, exactly as ADR 0023 decision 1
# requires. Refuses to overwrite a non-blank slug, except the four
# Amphenol corrections, applied explicitly and separately: the live rows
# read rdj1212/rdj2203/rdj32a3/rdj32u1 against models spelled RJD…, a
# transposition typo settled with Mike as exactly that.
#
# Historical models throughout (apps.get_model()) — matches
# 0014_retire_companions.py's precedent of declaring this migration's own
# copy of the constant rather than importing it from a management command,
# so a migration never depends on live application code whose shape can
# change out from under it. Every value here is already lowercase and
# DNS-legal, so the real model's own strip/lowercase/validate_dns_label
# normalisation is moot either way.
#
# Idempotent: re-running finds every targeted row already at its target
# value (hostname_slug no longer blank / no longer the typo'd value) and
# writes nothing.

from django.db import migrations

#: Copied from inventory/management/commands/import_prod_data.py's
#: HOSTNAME_SLUGS — see that module's own constant for the full citation of
#: where each value comes from (which are copied from live hand-set values,
#: which are corrected, and which are new).
HOSTNAME_SLUGS: dict[tuple[str, str], str] = {
    ("Allen & Heath", "SQ-5"): "sq5",
    ("Amphenol", "RJD1212-0050"): "rjd1212",
    ("Amphenol", "RJD2203-0050"): "rjd2203",
    ("Amphenol", "RJD32A3-0050"): "rjd32a3",
    ("Amphenol", "RJD32U1-0050"): "rjd32u1",
    ("Audinate", "AVIO-AO2"): "avioao2",
    ("DiGiCo", "DMI-DANTE"): "dmidante",
    ("DiGiCo", "SD11"): "sd11",
    ("DiGiCo", "SD12"): "sd12",
    ("DiGiCo", "SD9"): "sd9",
    ("Lab.Gruppen", "LM26"): "lm26",
    ("Lab.Gruppen", "LM44"): "lm44",
    ("Lab.Gruppen", "PLM20000Q"): "plm20q",
    ("Martin Audio", "IK-42"): "ik42",
    ("Martin Audio", "IK-81"): "ik81",
    ("Neutrik", "NA2-DLINE"): "na2dline",
    ("Radial", "DiNET DAN-RX"): "danrx",
    ("Radial", "DiNET DAN-TX"): "dantx",
    ("Yamaha", "DM3"): "dm3",
    ("Yamaha", "DM7-EX"): "dm7ex",
    ("Yamaha", "DM7C"): "dm7c",
    ("Yamaha", "Tio1608-D2"): "tio1608d2",
    ("Cisco", "SG300-10MP"): "sg300-10mp",
    ("Cisco", "SG300-26P"): "sg300-26p",
    ("Cisco", "SG350-10"): "sg350-10",
    ("TP-Link", "TL-SG108E"): "tl-sg108e",
}

#: The four Amphenol corrections (see HOSTNAME_SLUGS's docstring above) —
#: applied unconditionally to rows still holding the typo'd value, unlike
#: everything in HOSTNAME_SLUGS itself, which only ever fills a blank.
AMPHENOL_CORRECTIONS: dict[tuple[str, str], tuple[str, str]] = {
    ("Amphenol", "RJD1212-0050"): ("rdj1212", "rjd1212"),
    ("Amphenol", "RJD2203-0050"): ("rdj2203", "rjd2203"),
    ("Amphenol", "RJD32A3-0050"): ("rdj32a3", "rjd32a3"),
    ("Amphenol", "RJD32U1-0050"): ("rdj32u1", "rjd32u1"),
}


def seed_hostname_slugs(apps, schema_editor):
    NetworkSwitchType = apps.get_model("inventory", "NetworkSwitchType")
    NetworkDeviceType = apps.get_model("inventory", "NetworkDeviceType")
    alias = schema_editor.connection.alias

    for model_cls in (NetworkSwitchType, NetworkDeviceType):
        for (manufacturer, model_name), slug in HOSTNAME_SLUGS.items():
            model_cls.objects.using(alias).filter(
                manufacturer=manufacturer, model=model_name, hostname_slug=""
            ).update(hostname_slug=slug)

    for (manufacturer, model_name), (wrong, right) in AMPHENOL_CORRECTIONS.items():
        NetworkDeviceType.objects.using(alias).filter(
            manufacturer=manufacturer, model=model_name, hostname_slug=wrong
        ).update(hostname_slug=right)


class Migration(migrations.Migration):
    dependencies = [
        ("inventory", "0017_hostname_normalise"),
    ]

    operations = [
        migrations.RunPython(seed_hostname_slugs, reverse_code=migrations.RunPython.noop),
    ]
