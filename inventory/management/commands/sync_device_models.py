"""Sync the Device Models CSV onto an existing, populated database.

``import_prod_data`` (ADR 0026 decision 5) reads this same CSV, but only
once, at the start of the world: it **refuses to run if any Rack already
exists**, because its rack-creation order determines every rack's base
address and cannot be safely layered onto a partially-populated database
(see that command's own ``help`` text). That refusal is correct for the
importer's job, but it leaves a running deployment with no route at all to
get corrected descriptions (or, opt-in, hostname slugs) into
``NetworkDeviceModel`` rows that already exist — the CSV can be edited all
day and nothing downstream of the initial import will ever read it again.

This command is that route. It applies the same CSV to a live database, in
place, as many times as needed: match existing rows, update the fields that
changed, leave everything else untouched. It deliberately does **not**
reuse anything from ``import_prod_data.py`` itself — that module's
``_create_device_models()`` is one stage of a single, one-shot, transactional
import run and assumes a from-scratch world (``DEVICE_TYPES``, the
``DEVICE_MODEL_SLUGS`` seed catalog, an import identity user). This command
only reuses the CSV parser, ``_prod_import_csv.parse_device_models()``,
rather than writing a second parser for the same file format.

Note that parser is *not* the "pure I/O, no domain judgement" helper
``verify_prod_import.py``'s docstring still calls it: it decides which rows
count, skipping any with a blank manufacturer or model. That decision used
to be silent, which meant a dropped row was invisible to the importer *and*
to the verifier built to catch exactly that. It now warns with line numbers
and validates the header, so sharing it is safe — but the reason it is safe
is the warning, not an absence of judgement. Read its output.

**What this command will never do**, on purpose:

- **Create or delete ``NetworkDeviceModel`` rows.** Matching is a plain
  ``(manufacturer, model)`` dict lookup, the same as
  ``_create_device_models()`` uses — no fuzzy matching. A row in the CSV
  with no matching database row does nothing except get reported; a typo
  in the sheet must never silently mint a new model. Creating models is
  the importer's job.
- **Recompute hostnames.** ``hostname_slug`` (opt-in via ``--slugs``) feeds
  every computed hostname on every device of that model (ADR 0023). Editing
  it here only changes the stored abbreviation — existing devices keep
  whatever hostname they already have until someone explicitly recomputes
  them via ``NetworkDeviceAdmin``'s "Recompute hostname" admin action. This
  command says so, loudly, and reports how many devices are affected, but
  never triggers a recompute itself.

Idempotent: running it twice with the same CSV reports zero changes the
second time. Safe to run against a database with existing data because it
only ever calls ``save(update_fields=[...])`` on rows that actually changed,
inside one transaction, after ``full_clean()`` — so a bad cell in the sheet
(an over-long description, an illegal hostname slug) raises a
``CommandError`` naming exactly which row is wrong instead of writing a
partial result or leaking a raw traceback.
"""

from pathlib import Path
from typing import Any

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from inventory.models import NetworkDevice, NetworkDeviceModel

from ._prod_import_csv import DeviceModelRow, parse_device_models, read_csv_rows

#: How much of a changed description to show in the report before
#: truncating with an ellipsis — enough to recognize the row, short enough
#: that a paragraph-long description doesn't dominate the output.
_REPORT_TRUNCATE_LENGTH = 60


def _truncate(text: str, length: int = _REPORT_TRUNCATE_LENGTH) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1] + "…"


def _model_label(manufacturer: str, model: str) -> str:
    return f"{manufacturer} {model}"


class Command(BaseCommand):
    help = (
        "Sync the Device Models CSV (ADR 0026 decision 5) onto an existing database's "
        "NetworkDeviceModel rows, in place. Unlike import_prod_data (which refuses to run once "
        "any Rack exists), this is the route for correcting descriptions on a live deployment. "
        "Matches existing rows by (manufacturer, model) only — never creates or deletes a "
        "NetworkDeviceModel. Descriptions sync by default; pass --slugs to also sync "
        "hostname_slug, which feeds computed hostnames (ADR 0023) and does not itself recompute "
        "any device's hostname. Safe to re-run: idempotent, and --dry-run reports without writing."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "csv_path",
            help="Path to the Device Models CSV (Manufacturer, Model, Description, Hostname Slug).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report exactly what would change without writing anything.",
        )
        parser.add_argument(
            "--slugs",
            action="store_true",
            help=(
                "Also sync hostname_slug from the CSV (default: descriptions only). "
                "hostname_slug feeds computed hostnames (ADR 0023) — changing it does not "
                "recompute any existing device's hostname; see NetworkDeviceAdmin's "
                "'Recompute hostname' action for that."
            ),
        )

    def handle(self, *args: Any, **options: Any) -> None:
        csv_path = Path(options["csv_path"])
        dry_run: bool = options["dry_run"]
        sync_slugs: bool = options["slugs"]

        if not csv_path.is_file():
            raise CommandError(f"Missing Device Models CSV: {csv_path}")

        try:
            rows = parse_device_models(read_csv_rows(csv_path))
        except ValueError as exc:
            raise CommandError(f"{csv_path}: {exc}") from exc

        rows_by_key: dict[tuple[str, str], DeviceModelRow] = {
            (row.manufacturer, row.model): row for row in rows
        }
        models_by_key: dict[tuple[str, str], NetworkDeviceModel] = {
            (m.manufacturer, m.model): m for m in NetworkDeviceModel.objects.all()
        }

        if sync_slugs:
            self.stdout.write(
                self.style.WARNING(
                    "--slugs is syncing hostname_slug from the CSV. hostname_slug feeds every "
                    "computed hostname (ADR 0023) — this command does NOT recompute any existing "
                    "device's hostname. Devices of an updated model keep their current hostname "
                    "until someone runs NetworkDeviceAdmin's 'Recompute hostname' action on them."
                )
            )

        planned_updates: list[tuple[NetworkDeviceModel, dict[str, tuple[str, str]]]] = []
        unchanged_count = 0
        csv_only: list[DeviceModelRow] = []

        for key in sorted(rows_by_key):
            row = rows_by_key[key]
            device_model = models_by_key.get(key)
            if device_model is None:
                csv_only.append(row)
                continue

            changes: dict[str, tuple[str, str]] = {}
            if device_model.description != row.description:
                changes["description"] = (device_model.description, row.description)
            if sync_slugs:
                new_slug = row.hostname_slug.strip().lower() if row.hostname_slug else row.hostname_slug
                if device_model.hostname_slug != new_slug:
                    changes["hostname_slug"] = (device_model.hostname_slug, new_slug)

            if not changes:
                unchanged_count += 1
                continue

            for field, (_old, new) in changes.items():
                setattr(device_model, field, new)
            try:
                device_model.full_clean()
            except ValidationError as exc:
                detail = "; ".join(
                    f"{field}: {', '.join(messages)}" for field, messages in exc.message_dict.items()
                )
                raise CommandError(
                    f"{_model_label(row.manufacturer, row.model)!r}: invalid data ({detail})"
                ) from exc
            planned_updates.append((device_model, changes))

        db_only = [m for key, m in models_by_key.items() if key not in rows_by_key]

        # -- Report: updated ----------------------------------------------------
        if planned_updates:
            self.stdout.write(self.style.MIGRATE_HEADING("Updated:"))
            for device_model, changes in planned_updates:
                field_summaries = []
                for field, (old, new) in changes.items():
                    old_display = _truncate(old) if old else "(blank)"
                    new_display = _truncate(new) if new else "(blank)"
                    field_summaries.append(f"{field}: {old_display!r} -> {new_display!r}")
                self.stdout.write(
                    f"  {_model_label(device_model.manufacturer, device_model.model)}: "
                    + "; ".join(field_summaries)
                )
        else:
            self.stdout.write(self.style.MIGRATE_HEADING("Updated:") + " none")

        # -- Report: unchanged ----------------------------------------------------
        self.stdout.write(f"\nUnchanged: {unchanged_count}")

        # -- Report: DB rows with no CSV row --------------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nIn the database, no CSV row ({len(db_only)}):"))
        if db_only:
            for device_model in sorted(db_only, key=lambda m: (m.manufacturer, m.model)):
                self.stdout.write(f"  {_model_label(device_model.manufacturer, device_model.model)}")
        else:
            self.stdout.write("  none")

        # -- Report: CSV rows with no matching model ------------------------------
        self.stdout.write(self.style.MIGRATE_HEADING(f"\nIn the CSV, no such model ({len(csv_only)}):"))
        if csv_only:
            for row in csv_only:
                self.stdout.write(f"  {_model_label(row.manufacturer, row.model)}")
        else:
            self.stdout.write("  none")

        # -- Report: duplicate slugs across different models ----------------------
        if sync_slugs:
            resulting_slugs: dict[str, list[str]] = {}
            for device_model in models_by_key.values():
                slug = device_model.hostname_slug
                if not slug:
                    continue
                resulting_slugs.setdefault(slug, []).append(
                    _model_label(device_model.manufacturer, device_model.model)
                )
            duplicates = {slug: labels for slug, labels in resulting_slugs.items() if len(labels) > 1}
            if duplicates:
                self.stdout.write(self.style.WARNING("\nDuplicate hostname_slug across different models:"))
                for slug, labels in sorted(duplicates.items()):
                    self.stdout.write(self.style.WARNING(f"  {slug!r}: {', '.join(sorted(labels))}"))
                self.stdout.write(
                    self.style.WARNING(
                        "  Nothing enforces cross-model hostname_slug uniqueness — this is a "
                        "warning, not a refusal."
                    )
                )

        # -- Write phase -----------------------------------------------------------
        if not dry_run and planned_updates:
            with transaction.atomic():
                for device_model, changes in planned_updates:
                    device_model.save(update_fields=list(changes.keys()))

        # -- Closing summary ---------------------------------------------------------
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"\nSummary: {len(planned_updates)} updated, {unchanged_count} unchanged, "
                f"{len(db_only)} in database only, {len(csv_only)} in CSV only."
            )
        )

        if dry_run:
            self.stdout.write(self.style.WARNING("Dry run: nothing was written."))
        elif planned_updates:
            if sync_slugs:
                affected_by_model = {
                    (device_model.manufacturer, device_model.model): NetworkDevice.objects.filter(
                        device_type__device_model=device_model
                    ).count()
                    for device_model, changes in planned_updates
                    if "hostname_slug" in changes
                }
                total_affected_devices = sum(affected_by_model.values())
                if total_affected_devices:
                    self.stdout.write(
                        self.style.WARNING(
                            f"{total_affected_devices} existing device(s) across "
                            f"{len(affected_by_model)} updated model(s) now have a stale "
                            "computed hostname. Nothing was recomputed — use NetworkDeviceAdmin's "
                            "'Recompute hostname' action on them."
                        )
                    )
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(planned_updates)} change(s)."))
        else:
            self.stdout.write(self.style.SUCCESS("Nothing to write."))
