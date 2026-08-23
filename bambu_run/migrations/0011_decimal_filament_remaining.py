from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bambu_run", "0010_filament_current_printer"),
    ]

    operations = [
        migrations.AlterField(
            model_name="filament",
            name="remaining_percent",
            field=models.DecimalField(
                decimal_places=2,
                default=100,
                help_text="Estimated remaining filament (0-100%)",
                max_digits=5,
            ),
        ),
        migrations.AlterField(
            model_name="filament",
            name="remaining_weight_grams",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Calculated remaining weight",
                max_digits=8,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="filamentsnapshot",
            name="remain_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="filamentusage",
            name="starting_percent",
            field=models.DecimalField(
                decimal_places=2,
                help_text="Filament remaining % at job start",
                max_digits=5,
            ),
        ),
        migrations.AlterField(
            model_name="filamentusage",
            name="ending_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Filament remaining % at job end",
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="filamentusage",
            name="consumed_percent",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Amount consumed during print",
                max_digits=5,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="filamentusage",
            name="consumed_grams",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Estimated grams consumed",
                max_digits=8,
                null=True,
            ),
        ),
    ]
