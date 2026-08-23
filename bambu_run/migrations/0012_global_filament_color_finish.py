from django.db import migrations, models


def _derive_finish(*values):
    text = " ".join(str(value or "") for value in values).lower()
    finish_terms = [
        ("transparent", "Transparent"),
        ("translucent", "Translucent"),
        ("carbon fiber", "Carbon Fiber"),
        ("carbon-fiber", "Carbon Fiber"),
        ("cf", "Carbon Fiber"),
        ("matte", "Matte"),
        ("satin", "Satin"),
        ("silk", "Silk"),
        ("glossy", "Glossy"),
        ("metallic", "Metallic"),
        ("metal", "Metallic"),
        ("sparkle", "Sparkle"),
        ("glitter", "Sparkle"),
        ("galaxy", "Galaxy"),
        ("marble", "Marble"),
        ("glow", "Glow"),
        ("wood", "Wood"),
        ("filled", "Filled"),
        ("composite", "Filled"),
        ("dual", "Dual Color"),
        ("tri", "Tri Color"),
    ]
    for needle, finish in finish_terms:
        if needle in text:
            return finish
    return "Default"


def backfill_finish_and_dedupe(apps, schema_editor):
    FilamentColor = apps.get_model("bambu_run", "FilamentColor")

    for color in FilamentColor.objects.all().order_by("id"):
        color.finish = _derive_finish(
            color.filament_sub_type,
            color.filament_type,
            color.color_name,
        )
        color.save(update_fields=["finish"])

    seen = set()
    for color in FilamentColor.objects.all().order_by("id"):
        key = (color.finish, color.color_name, color.color_code)
        if key in seen:
            color.delete()
            continue
        seen.add(key)


class Migration(migrations.Migration):

    dependencies = [
        ("bambu_run", "0011_decimal_filament_remaining"),
    ]

    operations = [
        migrations.AddField(
            model_name="filamentcolor",
            name="finish",
            field=models.CharField(
                choices=[
                    ("Default", "Default"),
                    ("Matte", "Matte"),
                    ("Satin", "Satin"),
                    ("Silk", "Silk"),
                    ("Glossy", "Glossy"),
                    ("Transparent", "Transparent"),
                    ("Translucent", "Translucent"),
                    ("Metallic", "Metallic"),
                    ("Sparkle", "Sparkle / Glitter"),
                    ("Galaxy", "Galaxy"),
                    ("Marble", "Marble"),
                    ("Glow", "Glow"),
                    ("Wood", "Wood"),
                    ("Carbon Fiber", "Carbon Fiber"),
                    ("Filled", "Filled / Composite"),
                    ("Dual Color", "Dual Color"),
                    ("Tri Color", "Tri Color"),
                ],
                default="Default",
                help_text="Visual finish or appearance family (Default, Matte, Silk, Transparent, etc.)",
                max_length=32,
            ),
        ),
        migrations.RunPython(backfill_finish_and_dedupe, migrations.RunPython.noop),
        migrations.AlterModelOptions(
            name="filamentcolor",
            options={
                "ordering": ["finish", "color_name"],
                "verbose_name": "Filament Color",
                "verbose_name_plural": "Filament Colors",
            },
        ),
        migrations.RemoveIndex(
            model_name="filamentcolor",
            name="infrastruct_color_c_de04ed_idx",
        ),
        migrations.RemoveIndex(
            model_name="filamentcolor",
            name="infrastruct_filamen_0465c7_idx",
        ),
        migrations.AlterUniqueTogether(
            name="filamentcolor",
            unique_together=set(),
        ),
        migrations.RemoveField(
            model_name="filamentcolor",
            name="filament_type_fk",
        ),
        migrations.RemoveField(
            model_name="filamentcolor",
            name="filament_type",
        ),
        migrations.RemoveField(
            model_name="filamentcolor",
            name="filament_sub_type",
        ),
        migrations.RemoveField(
            model_name="filamentcolor",
            name="brand",
        ),
        migrations.AddIndex(
            model_name="filamentcolor",
            index=models.Index(
                fields=["finish", "color_name"],
                name="infra_color_finish_name_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="filamentcolor",
            index=models.Index(fields=["color_code"], name="infra_color_code_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="filamentcolor",
            unique_together={("finish", "color_name", "color_code")},
        ),
    ]
