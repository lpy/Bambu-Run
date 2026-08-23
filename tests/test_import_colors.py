import pytest

from bambu_run.management.commands.bambu_import_colors import Command
from bambu_run.models import FilamentColor


@pytest.mark.django_db
def test_import_colors_creates_global_finish_color(tmp_path):
    catalog = tmp_path / "PLA Silk.txt"
    catalog.write_text("Green\nHex:#00FF00\n", encoding="utf-8")

    created, skipped, errors = Command()._process_file(catalog, dry_run=False)

    assert (created, skipped, errors) == (1, 0, 0)
    color = FilamentColor.objects.get()
    assert color.finish == "Silk"
    assert color.display_name == "Silk:Green"
    assert color.color_code == "00FF00"


@pytest.mark.django_db
def test_import_colors_empty_file_returns_three_counters(tmp_path):
    catalog = tmp_path / "PLA Basic.txt"
    catalog.write_text("", encoding="utf-8")

    assert Command()._process_file(catalog, dry_run=False) == (0, 0, 0)
