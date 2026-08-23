from django.db import migrations, models
import django.db.models.deletion


def backfill_single_printer_location(apps, schema_editor):
    Printer = apps.get_model("bambu_run", "Printer")
    Filament = apps.get_model("bambu_run", "Filament")

    active_printers = list(
        Printer.objects.filter(category="threed_printer", is_active=True)[:2]
    )
    if len(active_printers) == 1:
        Filament.objects.filter(
            is_loaded_in_ams=True,
            current_printer__isnull=True,
        ).update(current_printer=active_printers[0])


class Migration(migrations.Migration):

    dependencies = [
        ("bambu_run", "0009_printer_category"),
    ]

    operations = [
        migrations.AddField(
            model_name="filament",
            name="current_printer",
            field=models.ForeignKey(
                blank=True,
                help_text="Printer whose AMS/external spool currently has this inventory spool loaded",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="loaded_filaments",
                to="bambu_run.printer",
            ),
        ),
        migrations.AddIndex(
            model_name="filament",
            index=models.Index(
                fields=["current_printer", "is_loaded_in_ams"],
                name="infra_fila_current_5eb7f3_idx",
            ),
        ),
        migrations.RunPython(backfill_single_printer_location, migrations.RunPython.noop),
    ]
