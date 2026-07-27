"""Add Printer.category so printer queries can be scoped away from other devices.

`infrastructure_device` is shared with host projects. In a standalone Bambu-Run
deployment bambu_run owns the table and the column must be created here. In a
host project like RAE the table was created by that project's own app and
already carries a `category` column, so creating it again would fail.
`AddFieldIfMissing` introspects the table and only emits DDL when needed; the
model state is updated either way.
"""

import django.db.models.manager
from django.db import migrations, models


class AddFieldIfMissing(migrations.AddField):
    """AddField that is a no-op at the database level if the column exists."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        model = to_state.apps.get_model(app_label, self.model_name)
        with schema_editor.connection.cursor() as cursor:
            existing = {
                column.name
                for column in schema_editor.connection.introspection.get_table_description(
                    cursor, model._meta.db_table
                )
            }
        if self.name in existing:
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        """Reverse the model state only, never the column.

        In a host project the column belongs to that project's own app — dropping
        it on reverse would break the host's device model. Leaving an unused
        column behind in a standalone rollback is the harmless side of this trade.
        """
        return


class Migration(migrations.Migration):

    dependencies = [
        ("bambu_run", "0008_printermetrics_nozzle_info"),
    ]

    operations = [
        AddFieldIfMissing(
            model_name="printer",
            name="category",
            field=models.CharField(
                default="threed_printer",
                help_text=(
                    "Device category. Always 'threed_printer' for printers — present "
                    "because host projects may share this table with other device types."
                ),
                max_length=50,
            ),
        ),
        migrations.AlterModelOptions(
            name="printer",
            options={
                "base_manager_name": "all_objects",
                "default_manager_name": "objects",
                "ordering": ["name"],
                "verbose_name": "Printer",
                "verbose_name_plural": "Printers",
            },
        ),
        migrations.AlterModelManagers(
            name="printer",
            managers=[
                ("all_objects", django.db.models.manager.Manager()),
                ("objects", django.db.models.manager.Manager()),
            ],
        ),
    ]
