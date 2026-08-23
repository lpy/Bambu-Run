import pytest
from decimal import Decimal
from django.urls import reverse
from django.utils import timezone

from bambu_run.forms import FilamentForm
from bambu_run.models import Printer, PrinterMetrics, Filament, FilamentColor, FilamentSnapshot


@pytest.fixture
def logged_in_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="tester", password="pw")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_dashboard_filaments_carry_ams_unit_info(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(device=printer, timestamp=timezone.now())
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS",
        type="PLA", remain_percent=80,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=128, ams_type="AMS HT",
        type="PA-CF", remain_percent=50,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    filaments = resp.context["stats"]["filaments"]
    assert len(filaments) == 2
    units = {(f["ams_unit_id"], f["ams_type"]) for f in filaments}
    assert units == {(0, "AMS"), (128, "AMS HT")}

    ams_units = resp.context["stats"]["ams_units"]
    assert ams_units == [
        {"ams_unit_id": 0, "ams_type": "AMS"},
        {"ams_unit_id": 128, "ams_type": "AMS HT"},
    ]


@pytest.mark.django_db
def test_filament_timeline_keeps_same_tray_id_units_separate(logged_in_client):
    from bambu_run.views import PrinterDashboardView

    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(device=printer, timestamp=timezone.now())
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS",
        type="PLA", sub_type="PLA Basic", color="FF0000", remain_percent=80,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=128, ams_type="AMS HT",
        type="PLA", sub_type="PLA Basic", color="FF0000", remain_percent=50,
    )

    from bambu_run.views import fetch_snapshots_by_metric

    view = PrinterDashboardView()
    metrics = list(PrinterMetrics.objects.filter(pk=metric.pk))
    timeline = view._prepare_filament_timeline(
        metrics, fetch_snapshots_by_metric(metrics)
    )

    assert len(timeline) == 2


@pytest.mark.django_db
def test_dashboard_renders_unit_pills_and_badges_with_multiple_units(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(device=printer, timestamp=timezone.now())
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS",
        type="PLA", color="FF0000FF", remain_percent=80,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=128, ams_type="AMS HT",
        type="PA-CF", color="00FF00FF", remain_percent=50,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    assert resp.status_code == 200
    html = resp.content.decode()
    assert "ams-filter-pills" in html
    assert "ams-badge-ams" in html
    assert "ams-badge-ams-ht" in html
    assert 'data-ams-unit-id="0"' in html
    assert 'data-ams-unit-id="128"' in html


@pytest.mark.django_db
def test_dashboard_groups_filaments_by_ams_unit(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now(),
        ams_units=[
            {"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 5, "temp": 22.5},
            {"unit_id": "128", "ams_type": "AMS HT", "humidity": 8, "temp": 60.0},
        ],
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS 2 Pro",
        type="ABS", remain_percent=80,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=1, ams_unit_id=0, ams_type="AMS 2 Pro",
        type="ABS", remain_percent=60,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=128, ams_type="AMS HT",
        type="PA-CF", remain_percent=50,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    groups = resp.context["stats"]["ams_groups"]
    assert len(groups) == 2

    ams2pro_group, ht_group = groups
    assert ams2pro_group["unit_id"] == 0
    assert ams2pro_group["label"] == "AMS 2 Pro (Unit 0)"
    assert ams2pro_group["humidity"] == 5
    assert ams2pro_group["temp"] == 22.5
    assert len(ams2pro_group["filaments"]) == 2

    assert ht_group["unit_id"] == 128
    assert ht_group["label"] == "AMS HT (Unit 128)"
    assert ht_group["humidity"] == 8
    assert len(ht_group["filaments"]) == 1


@pytest.mark.django_db
def test_dashboard_displays_ams_raw_humidity_as_rh(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now(),
        ams_units=[
            {
                "unit_id": "0",
                "ams_type": "AMS 2 Pro",
                "humidity": 2,
                "humidity_raw": 29,
                "temp": 36.7,
            },
        ],
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS 2 Pro",
        type="PLA", remain_percent=80,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    group = resp.context["stats"]["ams_groups"][0]
    assert group["humidity"] == 29
    assert group["humidity_level"] == 2

    html = resp.content.decode()
    assert "29%RH" in html
    assert "2%RH" not in html


@pytest.mark.django_db
def test_dashboard_uses_inventory_location_when_snapshot_unit_missing(logged_in_client):
    printer = Printer.objects.create(name="3DP-093-642", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now(),
        ams_units=[
            {"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 5, "temp": 22.5},
        ],
    )
    filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA+ 2.0",
        brand="SUNLU",
        color="White",
        color_hex="#FFFFFF",
        current_printer=printer,
        is_loaded_in_ams=True,
        current_tray_id=0,
        ams_unit_id=0,
        ams_type="AMS 2 Pro",
        remaining_percent=100,
    )
    FilamentSnapshot.objects.create(
        printer_metric=metric,
        filament=filament,
        tray_id=0,
        ams_unit_id=None,
        ams_type="",
        type="PLA",
        brand="SUNLU",
        color="FFFFFFFF",
        remain_percent=100,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    groups = resp.context["stats"]["ams_groups"]
    assert len(groups) == 1
    assert groups[0]["label"] == "AMS 2 Pro (Unit 0)"
    assert groups[0]["filaments"][0]["brand"] == "SUNLU"


@pytest.mark.django_db
def test_dashboard_renders_wide_and_compact_panels(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now(),
        ams_units=[
            {"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 5, "temp": 22.5},
            {"unit_id": "128", "ams_type": "AMS HT", "humidity": 8, "temp": 60.0},
        ],
    )
    for tray_id in range(4):
        FilamentSnapshot.objects.create(
            printer_metric=metric, tray_id=tray_id, ams_unit_id=0, ams_type="AMS 2 Pro",
            type="ABS", remain_percent=80,
        )
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=128, ams_type="AMS HT",
        type="PA-CF", remain_percent=50,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    html = resp.content.decode()
    assert "col-12 ams-group" in html        # wide group: col-12 only
    assert "col-lg-3 ams-group" in html      # compact group: col-lg-3
    assert "AMS 2 Pro (Unit 0)" in html
    assert "AMS HT (Unit 128)" in html


@pytest.mark.django_db
def test_dashboard_hides_unit_pills_with_single_unit(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    metric = PrinterMetrics.objects.create(device=printer, timestamp=timezone.now())
    FilamentSnapshot.objects.create(
        printer_metric=metric, tray_id=0, ams_unit_id=0, ams_type="AMS",
        type="PLA", color="FF0000FF", remain_percent=80,
    )

    resp = logged_in_client.get(
        reverse("bambu_run:printer_dashboard", kwargs={"pk": printer.pk})
    )

    assert resp.status_code == 200
    assert "ams-filter-pills" not in resp.content.decode()


@pytest.mark.django_db
def test_filament_form_uses_latest_ams_units_for_location_choices(logged_in_client):
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    PrinterMetrics.objects.create(
        device=printer,
        timestamp=timezone.now(),
        ams_units=[
            {"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 5, "temp": 22.5},
            {"unit_id": "128", "ams_type": "AMS HT", "humidity": 8, "temp": 60.0},
        ],
    )

    resp = logged_in_client.get(reverse("bambu_run:filament_create"))

    assert resp.status_code == 200
    form = resp.context["form"]
    assert (0, "Printer A - AMS 2 Pro (Unit 0)") in form.fields["ams_unit_id"].widget.choices
    assert (128, "Printer A - AMS HT (Unit 128)") in form.fields["ams_unit_id"].widget.choices
    assert '"printer_id": ' in resp.content.decode()
    assert '"tray_ids": [0]' in resp.content.decode()


@pytest.mark.django_db
def test_filament_form_color_options_include_managed_hex_code():
    FilamentColor.objects.create(
        color_code="FF6A13",
        color_name="Orange",
        finish="Silk",
    )

    form = FilamentForm()
    rendered_color_select = str(form["color"])

    assert 'data-color-hex="#FF6A13"' in rendered_color_select
    assert ">Silk:Orange</option>" in rendered_color_select


@pytest.mark.django_db
def test_filament_form_defaults_initial_and_remaining_weight():
    form = FilamentForm()

    assert form.fields["initial_weight_grams"].initial == 1000
    assert form.fields["remaining_percent"].initial == 100
    assert form.fields["remaining_weight_grams"].initial == 1000
    assert "readonly" not in str(form["remaining_weight_grams"])


@pytest.mark.django_db
def test_filament_form_preserves_edited_remaining_weight():
    form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1000",
        "remaining_percent": "100",
        "remaining_weight_grams": "333.33",
        "remaining_source": "weight",
    })

    assert form.is_valid(), form.errors
    filament = form.save(commit=False)
    assert filament.remaining_weight_grams == Decimal("333.33")
    assert filament.remaining_percent == Decimal("33.33")


@pytest.mark.django_db
def test_filament_form_updates_remaining_weight_from_percent():
    form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1200",
        "remaining_percent": "25.55",
        "remaining_weight_grams": "999",
        "remaining_source": "percent",
    })

    assert form.is_valid(), form.errors
    filament = form.save(commit=False)
    assert filament.remaining_percent == Decimal("25.55")
    assert filament.remaining_weight_grams == Decimal("306.60")


@pytest.mark.django_db
def test_filament_form_rejects_more_than_two_decimal_places():
    percent_form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1000",
        "remaining_percent": "33.333",
        "remaining_weight_grams": "333.33",
        "remaining_source": "percent",
    })

    assert not percent_form.is_valid()
    assert "remaining_percent" in percent_form.errors

    weight_form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1000",
        "remaining_percent": "33.33",
        "remaining_weight_grams": "333.333",
        "remaining_source": "weight",
    })

    assert not weight_form.is_valid()
    assert "remaining_weight_grams" in weight_form.errors


@pytest.mark.django_db
def test_filament_inventory_hides_sn_and_actions_are_not_sortable(logged_in_client):
    Filament.objects.create(
        tray_uuid="REAL-SN-123456",
        type="PLA",
        brand="SUNLU",
        color="Black",
        color_hex="#000000",
        initial_weight_grams=1000,
        remaining_percent=100,
        remaining_weight_grams=1000,
    )

    resp = logged_in_client.get(reverse("bambu_run:filament_list"))

    assert resp.status_code == 200
    html = resp.content.decode()
    assert ">SN<" not in html
    assert "REAL-SN-123456" not in html
    assert "sort=brand" in html
    assert "sort=last_used" in html
    assert "sort=actions" not in html


@pytest.mark.django_db
def test_filament_inventory_sorts_by_visible_columns(logged_in_client):
    Filament.objects.create(
        type="PLA",
        brand="ZZZ",
        color="Black",
        color_hex="#000000",
        initial_weight_grams=1000,
        remaining_percent=80,
        remaining_weight_grams=800,
    )
    Filament.objects.create(
        type="PETG",
        brand="AAA",
        color="White",
        color_hex="#FFFFFF",
        initial_weight_grams=1000,
        remaining_percent=20,
        remaining_weight_grams=200,
    )

    resp = logged_in_client.get(reverse("bambu_run:filament_list") + "?sort=brand&dir=asc")
    assert [filament.brand for filament in resp.context["filaments"]] == ["AAA", "ZZZ"]

    resp = logged_in_client.get(reverse("bambu_run:filament_list") + "?sort=remaining&dir=desc")
    assert [filament.remaining_percent for filament in resp.context["filaments"]] == [80, 20]


@pytest.mark.django_db
def test_filament_inventory_persists_sort_through_filters_and_pagination(logged_in_client):
    for idx in range(21):
        Filament.objects.create(
            type="PLA",
            brand=f"Brand {idx:02d}",
            color="Black",
            color_hex="#000000",
            initial_weight_grams=1000,
            remaining_percent=100,
            remaining_weight_grams=1000,
        )

    resp = logged_in_client.get(
        reverse("bambu_run:filament_list") + "?sort=brand&dir=asc&type=PLA"
    )

    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'name="sort" value="brand"' in html
    assert 'name="dir" value="asc"' in html
    assert resp.context["page_urls"]["next"] == "?sort=brand&dir=asc&type=PLA&page=2"


@pytest.mark.django_db
def test_loaded_filament_requires_ams_unit_and_tray():
    printer = Printer.objects.create(name="Printer A", model="H2C", is_active=True)
    form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "sub_type": "PLA Basic",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1000",
        "remaining_percent": "100",
        "is_loaded_in_ams": "on",
        "current_printer": str(printer.pk),
        "current_tray_id": "0",
    })

    assert not form.is_valid()
    assert "AMS Unit ID required when filament is loaded in AMS" in str(form.errors)


@pytest.mark.django_db
def test_loaded_filament_requires_printer_location():
    form = FilamentForm(data={
        "created_by": "Manual",
        "type": "PLA",
        "sub_type": "PLA Basic",
        "brand": "SUNLU",
        "color": "Black",
        "color_hex": "#000000",
        "diameter": "1.75",
        "initial_weight_grams": "1000",
        "remaining_percent": "100",
        "is_loaded_in_ams": "on",
        "ams_unit_id": "0",
        "current_tray_id": "0",
    })

    assert not form.is_valid()
    assert "Printer required when filament is loaded in AMS" in str(form.errors)
