"""Guards against the query-count and payload regressions that made the printer
pages slow: deferred-field N+1s, unbounded date ranges, and unsampled chart data.
"""

import json
from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from bambu_run.models import Printer, PrinterMetrics, FilamentSnapshot
from bambu_run.views import _MAX_CHART_POINTS


@pytest.fixture
def logged_in_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="perf", password="pw")
    client.force_login(user)
    return client


@pytest.fixture
def printer():
    return Printer.objects.create(name="Perf Printer", model="H2C", is_active=True)


def _make_metrics(printer, count, *, snapshots_per_metric=2, spacing_seconds=30):
    """Create `count` metrics ending now, each with some filament snapshots."""
    now = timezone.now()
    metrics = PrinterMetrics.objects.bulk_create(
        [
            PrinterMetrics(
                device=printer,
                timestamp=now - timedelta(seconds=spacing_seconds * (count - i)),
                nozzle_temp=200 + i % 5,
                nozzle_target_temp=220,
                nozzle_temp_left=180 + i % 3,
                nozzle_target_temp_left=190,
                bed_temp=60,
                bed_target_temp=60,
                print_percent=i % 100,
                gcode_state="RUNNING",
                print_type="local",
                subtask_name="job",
            )
            for i in range(count)
        ]
    )
    FilamentSnapshot.objects.bulk_create(
        [
            FilamentSnapshot(
                printer_metric=m,
                tray_id=str(tray),
                type="PLA",
                sub_type="Bambu",
                color="FF0000FF",
                remain_percent=80,
            )
            for m in metrics
            for tray in range(snapshots_per_metric)
        ]
    )
    return metrics


# --- Root cause 1: deferred-field N+1 in the API -----------------------------


def _count_queries(client, url, params=None):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(url, params or {})
    assert resp.status_code == 200
    return len(ctx)


@pytest.mark.django_db
def test_api_query_count_is_independent_of_row_count(logged_in_client, printer):
    """Every field the serializer reads must be in .only(), or Django emits one
    extra SELECT per row per missing field — making query count scale with data."""
    today = timezone.localtime().date()
    url = reverse("bambu_run:printer_api")
    params = {
        "start_date": str(today - timedelta(days=1)),
        "end_date": str(today),
        "start_time": "00:00",
        "end_time": "23:59",
    }

    _make_metrics(printer, 10)
    few = _count_queries(logged_in_client, url, params)

    _make_metrics(printer, 190)
    many = _count_queries(logged_in_client, url, params)

    assert few == many, f"query count scales with rows: {few} -> {many}"


@pytest.mark.django_db
def test_api_returns_dual_nozzle_values(logged_in_client, printer):
    """The left-nozzle fields must survive the .only() narrowing."""
    _make_metrics(printer, 5)
    today = timezone.localtime().date()

    resp = logged_in_client.get(
        reverse("bambu_run:printer_api"),
        {
            "start_date": str(today - timedelta(days=1)),
            "end_date": str(today),
            "start_time": "00:00",
            "end_time": "23:59",
        },
    )

    data = resp.json()
    assert any(v is not None for v in data["nozzle_temp_left"])
    assert any(v is not None for v in data["nozzle_target_temp_left"])


# --- Root cause: unbounded query when date params are missing ----------------


@pytest.mark.django_db
def test_api_without_params_is_time_bounded(logged_in_client, printer):
    """A bare API call must not scan the whole table — it defaults to 24h."""
    _make_metrics(printer, 10, spacing_seconds=30)  # inside 24h
    old = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now() - timedelta(days=30), nozzle_temp=100
    )

    resp = logged_in_client.get(reverse("bambu_run:printer_api"))

    data = resp.json()
    assert len(data["timestamps"]) == 10
    assert old.timestamp.isoformat() not in data["timestamps_iso"]


@pytest.mark.django_db
def test_api_with_only_start_date_is_time_bounded(logged_in_client, printer):
    """Partial params must not drop the upper bound and scan forever."""
    _make_metrics(printer, 5)
    today = timezone.localtime().date()

    resp = logged_in_client.get(
        reverse("bambu_run:printer_api"), {"start_date": str(today - timedelta(days=1))}
    )

    assert resp.status_code == 200
    assert len(resp.json()["timestamps"]) == 5


@pytest.mark.django_db
def test_api_downsamples_above_max_chart_points(logged_in_client, printer, monkeypatch):
    monkeypatch.setattr("bambu_run.views._MAX_CHART_POINTS", 10)
    _make_metrics(printer, 40, snapshots_per_metric=1, spacing_seconds=30)
    today = timezone.localtime().date()

    resp = logged_in_client.get(
        reverse("bambu_run:printer_api"),
        {
            "start_date": str(today - timedelta(days=1)),
            "end_date": str(today),
            "start_time": "00:00",
            "end_time": "23:59",
        },
    )

    assert 0 < len(resp.json()["timestamps"]) <= 10


# --- Root cause 2: the dashboard render -------------------------------------


@pytest.mark.django_db
def test_dashboard_query_count_is_independent_of_row_count(logged_in_client, printer):
    url = reverse("bambu_run:printer_dashboard")

    _make_metrics(printer, 10)
    few = _count_queries(logged_in_client, url)

    _make_metrics(printer, 190)
    many = _count_queries(logged_in_client, url)

    assert few == many, f"query count scales with rows: {few} -> {many}"


@pytest.mark.django_db
def test_dashboard_downsamples_chart_payload(logged_in_client, printer, monkeypatch):
    """The dashboard inlines its JSON into the HTML, so it must sample like the API."""
    monkeypatch.setattr("bambu_run.views._MAX_CHART_POINTS", 10)
    _make_metrics(printer, 60, snapshots_per_metric=1)

    resp = logged_in_client.get(reverse("bambu_run:printer_dashboard"))
    payload = json.loads(resp.context["printer_data_json"])

    assert 0 < len(payload["timestamps"]) <= 10


@pytest.mark.django_db
def test_dashboard_stats_use_the_newest_metric(logged_in_client, printer):
    """Sampling must never drop the latest reading — the stat cards depend on it."""
    import zoneinfo

    from bambu_run.conf import app_settings

    _make_metrics(printer, 20)
    newest = PrinterMetrics.objects.create(
        device=printer, timestamp=timezone.now(), nozzle_temp=242, gcode_state="RUNNING"
    )

    resp = logged_in_client.get(reverse("bambu_run:printer_dashboard"))

    assert resp.context["stats"]["nozzle_temp"] == pytest.approx(242)
    assert resp.context["stats"]["timestamp"] == newest.timestamp.astimezone(
        zoneinfo.ZoneInfo(app_settings.TIMEZONE)
    ).strftime("%Y-%m-%d %H:%M:%S")


@pytest.mark.django_db
def test_dashboard_filament_timeline_aligns_with_timestamps(logged_in_client, printer):
    """remain_data must stay index-aligned with timestamps after sampling."""
    _make_metrics(printer, 30, snapshots_per_metric=2)

    resp = logged_in_client.get(reverse("bambu_run:printer_dashboard"))
    payload = json.loads(resp.context["printer_data_json"])

    n = len(payload["timestamps"])
    assert payload["filament_timeline"]
    for series in payload["filament_timeline"].values():
        assert len(series["remain_data"]) == n
