from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bambu_run", "0012_global_filament_color_finish"),
    ]

    operations = [
        migrations.AddField(
            model_name="filament",
            name="is_loaded_externally",
            field=models.BooleanField(
                default=False,
                help_text="Is this spool currently loaded on the printer's external spool holder?",
            ),
        ),
        migrations.AddIndex(
            model_name="filament",
            index=models.Index(
                fields=["current_printer", "is_loaded_externally"],
                name="infra_fila_external_20d56d_idx",
            ),
        ),
    ]
