from decimal import Decimal

import pytest
from django.test import override_settings

from bambu_run.management.commands.bambu_collector import Command, DeviceSession, resolve_printer_device
from bambu_run.models import Filament, FilamentSnapshot, FilamentUsage, PrinterMetrics


class FakeClient:
    """Stub in place of BambuPrinter — returns canned snapshots, no real MQTT."""

    def __init__(self, snapshots, cloud_client=None):
        self._snapshots = snapshots
        self._index = 0
        self._client = cloud_client

    def get_snapshot(self):
        snap = self._snapshots[min(self._index, len(self._snapshots) - 1)]
        self._index += 1
        return snap


class FakeCloudClient:
    def __init__(self, tasks):
        self._tasks = tasks

    def get(self, path, params=None):
        return {"hits": self._tasks}


def make_session(device_id, name, snapshots, cloud_client=None):
    printer = resolve_printer_device(device_id, {"name": name, "dev_product_name": "H2C"})
    return DeviceSession(
        device_id=device_id,
        client=FakeClient(snapshots, cloud_client=cloud_client),
        printer=printer,
    )


def two_unit_tray0_snapshot():
    """Two AMS units (AMS unit_id=0, AMS HT unit_id=128) both report tray_id=0,
    with different filament types loaded — these must not collide."""
    return {
        "gcode_state": "IDLE",
        "ams_units": [
            {"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0},
            {"unit_id": "128", "ams_type": "AMS HT", "humidity": 20, "temp": 60.0},
        ],
        "filaments": [
            {
                "tray_id": 0, "type": "PLA", "sub_type": "PLA Basic", "color": "FF0000FF",
                "tray_uuid": "UUID-UNIT0-TRAY0",
                "remain_percent": 80, "ams_unit_id": 0, "ams_type": "AMS",
            },
            {
                "tray_id": 0, "type": "PA-CF", "sub_type": "PA6-CF", "color": "00FF00FF",
                "tray_uuid": "UUID-UNIT128-TRAY0",
                "remain_percent": 50, "ams_unit_id": 128, "ams_type": "AMS HT",
            },
        ],
    }


@pytest.mark.django_db
def test_two_ams_units_with_same_tray_id_create_distinct_snapshots():
    session = make_session("SERIAL-A", "Printer A", [two_unit_tray0_snapshot()])

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)

    metric = PrinterMetrics.objects.get(device=session.printer)
    snapshots = FilamentSnapshot.objects.filter(printer_metric=metric).order_by("ams_unit_id")

    assert snapshots.count() == 2

    ams_snap, ht_snap = snapshots
    assert ams_snap.tray_id == 0
    assert ams_snap.ams_unit_id == 0
    assert ams_snap.ams_type == "AMS"
    assert ams_snap.type == "PLA"

    assert ht_snap.tray_id == 0
    assert ht_snap.ams_unit_id == 128
    assert ht_snap.ams_type == "AMS HT"
    assert ht_snap.type == "PA-CF"


@pytest.mark.django_db
def test_collector_skips_blank_default_snapshot():
    session = make_session("SERIAL-A", "Printer A", [{}])

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)

    assert PrinterMetrics.objects.filter(device=session.printer).count() == 0


@pytest.mark.django_db
def test_filament_usage_matches_correct_unit_when_tray_ids_collide():
    start_snapshot = two_unit_tray0_snapshot()
    start_snapshot.update({"gcode_state": "RUNNING", "subtask_name": "job_1", "print_percent": 1, "tray_now": "0"})

    end_snapshot = two_unit_tray0_snapshot()
    end_snapshot["filaments"][0]["remain_percent"] = 70  # AMS unit 0 consumed
    end_snapshot["filaments"][1]["remain_percent"] = 50  # AMS HT unit 128 untouched
    end_snapshot.update({"gcode_state": "FINISH", "subtask_name": "job_1", "print_percent": 100})

    session = make_session("SERIAL-A", "Printer A", [start_snapshot, end_snapshot])

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)
    cmd._collect_printer_data(session)

    usages = FilamentUsage.objects.filter(print_job__device=session.printer).order_by("ams_unit_id")
    # Both units reported tray_id=0 with a tracked filament loaded throughout the
    # job — usage is recorded per physical unit, not collapsed into one ambiguous row.
    assert usages.count() == 2

    ams_usage, ht_usage = usages
    assert ams_usage.ams_unit_id == 0
    assert ams_usage.starting_percent == 80
    assert ams_usage.ending_percent == 70

    assert ht_usage.ams_unit_id == 128
    assert ht_usage.starting_percent == 50
    assert ht_usage.ending_percent == 50


@pytest.mark.django_db
def test_manually_loaded_third_party_filament_creates_slot_snapshot_without_mqtt_filament():
    session = make_session(
        "SERIAL-A",
        "Printer A",
        [{
            "gcode_state": "IDLE",
            "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
            "filaments": [],
        }],
    )
    filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA Basic",
        brand="SUNLU",
        color="Black",
        color_hex="#000000",
        initial_weight_grams=1000,
        remaining_percent=90,
        remaining_weight_grams=900,
        current_printer=session.printer,
        is_loaded_in_ams=True,
        current_tray_id=0,
        ams_unit_id=0,
        ams_type="AMS",
    )

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)

    snapshot = FilamentSnapshot.objects.get(printer_metric__device=session.printer)
    assert snapshot.filament == filament
    assert snapshot.match_method == "manual_loaded_slot"
    assert snapshot.type == "PLA"
    assert snapshot.brand == "SUNLU"
    assert snapshot.remain_percent == 90


@pytest.mark.django_db
def test_reported_trays_and_manual_slot_share_same_ams_unit_snapshot_group():
    session = make_session(
        "SERIAL-A",
        "Printer A",
        [{
            "gcode_state": "IDLE",
            "ams_units": [{"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 30, "temp": 25.0}],
            "filaments": [
                {
                    "tray_id": 0, "type": "PLA", "sub_type": "PLA+ 2.0", "color": "FFFFFFFF",
                    "remain_percent": 100,
                },
                {
                    "tray_id": 1, "type": "PETG", "sub_type": "Basic", "color": "000000FF",
                    "remain_percent": -1, "ams_unit_id": 0, "ams_type": "AMS 2 Pro",
                },
                {
                    "tray_id": 2, "type": "PETG", "sub_type": "Basic", "color": "FFFFFFFF",
                    "remain_percent": 80, "ams_unit_id": 0, "ams_type": "AMS 2 Pro",
                },
                {
                    "tray_id": 3, "type": "PLA", "sub_type": "PLA+ 2.0", "color": "FFFFFFFF",
                    "remain_percent": -1, "ams_unit_id": 0, "ams_type": "AMS 2 Pro",
                },
            ],
        }],
    )
    manual_filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA+ 2.0",
        brand="SUNLU",
        color="White",
        color_hex="#FFFFFF",
        initial_weight_grams=1000,
        remaining_percent=100,
        remaining_weight_grams=1000,
        current_printer=session.printer,
        is_loaded_in_ams=True,
        current_tray_id=0,
        ams_unit_id=0,
        ams_type="AMS 2 Pro",
    )

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)

    snapshots = FilamentSnapshot.objects.filter(
        printer_metric__device=session.printer,
    ).order_by("tray_id")

    assert snapshots.count() == 4
    assert {snap.ams_unit_id for snap in snapshots} == {0}
    assert {snap.ams_type for snap in snapshots} == {"AMS 2 Pro"}
    assert snapshots[0].filament == manual_filament
    assert snapshots[0].match_method == "manual_loaded_slot"
    assert snapshots[1].filament is not None
    assert snapshots[1].remain_percent is None
    assert snapshots[1].filament.remaining_percent == 100


@pytest.mark.django_db
def test_placeholder_tray_uuid_does_not_break_auto_create_for_multiple_trays():
    session = make_session(
        "SERIAL-A",
        "Printer A",
        [{
            "gcode_state": "IDLE",
            "ams_units": [{"unit_id": "0", "ams_type": "AMS 2 Pro", "humidity": 30, "temp": 25.0}],
            "filaments": [
                {
                    "tray_id": 0, "type": "PLA", "sub_type": "Basic", "color": "000000FF",
                    "tray_uuid": "00000000000000000000000000000000",
                    "tag_uid": "00:00:00:00",
                    "remain_percent": 100, "ams_unit_id": 0, "ams_type": "AMS 2 Pro",
                },
                {
                    "tray_id": 1, "type": "PETG", "sub_type": "Basic", "color": "FFFFFFFF",
                    "tray_uuid": "00000000-0000-0000-0000-000000000000",
                    "tag_uid": "0000000000000000",
                    "remain_percent": 95, "ams_unit_id": 0, "ams_type": "AMS 2 Pro",
                },
            ],
        }],
    )

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)

    snapshots = FilamentSnapshot.objects.filter(
        printer_metric__device=session.printer,
    ).order_by("tray_id")
    filaments = Filament.objects.order_by("current_tray_id")

    assert snapshots.count() == 2
    assert filaments.count() == 2
    assert {snapshot.tray_uuid for snapshot in snapshots} == {None}
    assert {snapshot.tag_uid for snapshot in snapshots} == {None}
    assert {filament.tray_uuid for filament in filaments} == {None}
    assert {filament.tag_uid for filament in filaments} == {None}
    assert [filament.current_tray_id for filament in filaments] == [0, 1]


@pytest.mark.django_db
def test_cloud_task_weight_deducts_manually_loaded_third_party_filament():
    ams_snapshot = {
        "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
        "filaments": [],
        "tray_now": "0",
        "task_id": "123",
        "subtask_name": "third_party_job",
    }
    start_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 1}
    end_snapshot = {**ams_snapshot, "gcode_state": "FINISH", "print_percent": 100}
    cloud_client = FakeCloudClient([
        {
            "id": 123,
            "title": "third_party_job",
            "deviceId": "SERIAL-A",
            "status": 2,
            "weight": 42.4,
            "useAms": True,
            "amsDetailMapping": [
                {"amsId": 0, "slotId": 0, "weight": 42.4, "filamentType": "PLA"}
            ],
        }
    ])
    session = make_session(
        "SERIAL-A",
        "Printer A",
        [start_snapshot, end_snapshot],
        cloud_client=cloud_client,
    )
    filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA Basic",
        brand="SUNLU",
        color="Black",
        color_hex="#000000",
        initial_weight_grams=1000,
        remaining_percent=100,
        remaining_weight_grams=1000,
        current_printer=session.printer,
        is_loaded_in_ams=True,
        current_tray_id=0,
        ams_unit_id=0,
        ams_type="AMS",
    )

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session)
    cmd._collect_printer_data(session)

    usage = FilamentUsage.objects.get(print_job__device=session.printer)
    filament.refresh_from_db()

    assert usage.filament == filament
    assert usage.consumed_grams == Decimal("42.40")
    assert usage.consumed_percent == Decimal("4.24")
    assert usage.ending_percent == Decimal("95.76")
    assert filament.remaining_weight_grams == Decimal("957.60")
    assert filament.remaining_percent == Decimal("95.76")


@pytest.mark.django_db
def test_local_gcode_usage_deducts_manually_loaded_third_party_filament(tmp_path):
    gcode_file = tmp_path / "third_party_job.gcode"
    gcode_file.write_text("; filament used [g] = 50.0\n", encoding="utf-8")
    ams_snapshot = {
        "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
        "filaments": [],
        "tray_now": "0",
        "gcode_file": "third_party_job.gcode",
        "subtask_name": "third_party_job",
    }
    start_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 1}
    end_snapshot = {**ams_snapshot, "gcode_state": "FINISH", "print_percent": 100}
    session = make_session("SERIAL-A", "Printer A", [start_snapshot, end_snapshot])
    filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA Basic",
        brand="SUNLU",
        color="Black",
        color_hex="#000000",
        initial_weight_grams=1000,
        remaining_percent=100,
        remaining_weight_grams=1000,
        current_printer=session.printer,
        is_loaded_in_ams=True,
        current_tray_id=0,
        ams_unit_id=0,
        ams_type="AMS",
    )

    cmd = Command()
    cmd.verbose = False
    with override_settings(BAMBU_RUN_PRINT_FILE_DIRS=[str(tmp_path)]):
        cmd._collect_printer_data(session)
        cmd._collect_printer_data(session)

    usage = FilamentUsage.objects.get(print_job__device=session.printer)
    filament.refresh_from_db()

    assert usage.filament == filament
    assert usage.consumed_grams == 50
    assert usage.consumed_percent == 5
    assert usage.ending_percent == 95
    assert filament.remaining_weight_grams == 950
    assert filament.remaining_percent == 95


@pytest.mark.django_db
def test_external_spool_snapshot_and_local_gcode_usage(tmp_path):
    gcode_file = tmp_path / "external_job.gcode"
    gcode_file.write_text("; filament used [g] = 12.34\n", encoding="utf-8")
    base_snapshot = {
        "external_spool": {
            "tray_id": "254",
            "type": "PLA",
            "sub_type": "PLA Basic",
            "color": "FFFFFFFF",
            "remain_percent": 0,
            "is_external": True,
        },
        "tray_now": "254",
        "gcode_file": "external_job.gcode",
        "subtask_name": "external_job",
    }
    start_snapshot = {**base_snapshot, "gcode_state": "RUNNING", "print_percent": 1}
    end_snapshot = {**base_snapshot, "gcode_state": "FINISH", "print_percent": 100}
    session = make_session("SERIAL-A", "Printer A", [start_snapshot, end_snapshot])
    filament = Filament.objects.create(
        type="PLA",
        sub_type="PLA Basic",
        brand="SUNLU",
        color="White",
        color_hex="#FFFFFF",
        initial_weight_grams=1000,
        remaining_percent=100,
        remaining_weight_grams=1000,
        current_printer=session.printer,
        is_loaded_externally=True,
        current_tray_id=254,
    )

    cmd = Command()
    cmd.verbose = False
    with override_settings(BAMBU_RUN_PRINT_FILE_DIRS=[str(tmp_path)]):
        cmd._collect_printer_data(session)
        cmd._collect_printer_data(session)

    snapshots = FilamentSnapshot.objects.filter(filament=filament).order_by("printer_metric__timestamp")
    assert snapshots.count() == 2
    assert {snapshot.tray_id for snapshot in snapshots} == {254}
    assert {snapshot.match_method for snapshot in snapshots} == {"manual_loaded_external"}

    usage = FilamentUsage.objects.get(print_job__device=session.printer)
    filament.refresh_from_db()

    assert usage.tray_id == 254
    assert usage.ams_unit_id is None
    assert usage.consumed_grams == Decimal("12.34")
    assert filament.remaining_weight_grams == Decimal("987.66")
    assert filament.remaining_percent == Decimal("98.77")


@pytest.mark.django_db
def test_mid_print_filament_change_with_unknown_leftover_charges_replacement_from_gcode_line(tmp_path):
    gcode_file = tmp_path / "runout_job.gcode"
    gcode_file.write_text(
        "\n".join([
            "; filament used [g] = 200.0",
            "M82",
            "G1 X0 E25.0",
            "G1 X1 E60.0",
            "G1 X2 E100.0",
        ]),
        encoding="utf-8",
    )
    ams_snapshot = {
        "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
        "filaments": [],
        "tray_now": "0",
        "gcode_file": "runout_job.gcode",
        "subtask_name": "runout_job",
    }
    start_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 1, "print_line_number": 2}
    change_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 60, "print_line_number": 4}
    end_snapshot = {**ams_snapshot, "gcode_state": "FINISH", "print_percent": 100, "print_line_number": 5}
    session = make_session("SERIAL-A", "Printer A", [start_snapshot, change_snapshot, end_snapshot])
    old_filament = Filament.objects.create(
        type="PLA", sub_type="PLA Basic", brand="SUNLU", color="Black", color_hex="#000000",
        initial_weight_grams=1000, remaining_percent=100, remaining_weight_grams=None,
        current_printer=session.printer, is_loaded_in_ams=True,
        current_tray_id=0, ams_unit_id=0, ams_type="AMS",
    )
    replacement = Filament.objects.create(
        type="PLA", sub_type="PLA Basic", brand="SUNLU", color="White", color_hex="#FFFFFF",
        initial_weight_grams=1000, remaining_percent=100, remaining_weight_grams=1000,
    )

    cmd = Command()
    cmd.verbose = False
    with override_settings(BAMBU_RUN_PRINT_FILE_DIRS=[str(tmp_path)]):
        cmd._collect_printer_data(session)
        old_filament.is_loaded_in_ams = False
        old_filament.current_printer = None
        old_filament.current_tray_id = None
        old_filament.ams_unit_id = None
        old_filament.ams_type = ""
        old_filament.save()
        replacement.current_printer = session.printer
        replacement.is_loaded_in_ams = True
        replacement.current_tray_id = 0
        replacement.ams_unit_id = 0
        replacement.ams_type = "AMS"
        replacement.save()
        cmd._collect_printer_data(session)
        cmd._collect_printer_data(session)

    usages = list(FilamentUsage.objects.filter(print_job__device=session.printer).order_by("id"))
    old_filament.refresh_from_db()
    replacement.refresh_from_db()

    assert len(usages) == 2
    assert usages[0].filament == old_filament
    assert usages[0].consumed_grams is None
    assert usages[0].ending_percent == 0
    assert old_filament.remaining_weight_grams == 0
    assert old_filament.remaining_percent == 0

    assert usages[1].filament == replacement
    assert usages[1].consumed_grams == 80
    assert replacement.remaining_weight_grams == 920
    assert replacement.remaining_percent == 92


@pytest.mark.django_db
def test_mid_print_filament_change_with_known_leftover_charges_known_runout_amount(tmp_path):
    gcode_file = tmp_path / "known_runout_job.gcode"
    gcode_file.write_text(
        "\n".join([
            "; filament used [g] = 200.0",
            "M82",
            "G1 X0 E25.0",
            "G1 X1 E60.0",
            "G1 X2 E100.0",
        ]),
        encoding="utf-8",
    )
    ams_snapshot = {
        "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
        "filaments": [],
        "tray_now": "0",
        "gcode_file": "known_runout_job.gcode",
        "subtask_name": "known_runout_job",
    }
    start_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 1, "print_line_number": 2}
    change_snapshot = {**ams_snapshot, "gcode_state": "RUNNING", "print_percent": 60, "print_line_number": 4}
    end_snapshot = {**ams_snapshot, "gcode_state": "FINISH", "print_percent": 100, "print_line_number": 5}
    session = make_session("SERIAL-A", "Printer A", [start_snapshot, change_snapshot, end_snapshot])
    old_filament = Filament.objects.create(
        type="PLA", sub_type="PLA Basic", brand="SUNLU", color="Black", color_hex="#000000",
        initial_weight_grams=1000, remaining_percent=5, remaining_weight_grams=50,
        current_printer=session.printer, is_loaded_in_ams=True,
        current_tray_id=0, ams_unit_id=0, ams_type="AMS",
    )
    replacement = Filament.objects.create(
        type="PLA", sub_type="PLA Basic", brand="SUNLU", color="White", color_hex="#FFFFFF",
        initial_weight_grams=1000, remaining_percent=100, remaining_weight_grams=1000,
    )

    cmd = Command()
    cmd.verbose = False
    with override_settings(BAMBU_RUN_PRINT_FILE_DIRS=[str(tmp_path)]):
        cmd._collect_printer_data(session)
        old_filament.is_loaded_in_ams = False
        old_filament.current_printer = None
        old_filament.current_tray_id = None
        old_filament.ams_unit_id = None
        old_filament.ams_type = ""
        old_filament.save()
        replacement.current_printer = session.printer
        replacement.is_loaded_in_ams = True
        replacement.current_tray_id = 0
        replacement.ams_unit_id = 0
        replacement.ams_type = "AMS"
        replacement.save()
        cmd._collect_printer_data(session)
        cmd._collect_printer_data(session)

    usages = list(FilamentUsage.objects.filter(print_job__device=session.printer).order_by("id"))
    old_filament.refresh_from_db()
    replacement.refresh_from_db()

    assert len(usages) == 2
    assert usages[0].filament == old_filament
    assert usages[0].consumed_grams == 50
    assert old_filament.remaining_weight_grams == 0
    assert old_filament.remaining_percent == 0

    assert usages[1].filament == replacement
    assert usages[1].consumed_grams == 150
    assert replacement.remaining_weight_grams == 850
    assert replacement.remaining_percent == 85


@pytest.mark.django_db
def test_manual_loaded_slots_are_scoped_by_printer():
    session_a = make_session(
        "SERIAL-A",
        "Printer A",
        [{
            "gcode_state": "IDLE",
            "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 30, "temp": 25.0}],
            "filaments": [],
        }],
    )
    session_b = make_session(
        "SERIAL-B",
        "Printer B",
        [{
            "gcode_state": "IDLE",
            "ams_units": [{"unit_id": "0", "ams_type": "AMS", "humidity": 35, "temp": 24.0}],
            "filaments": [],
        }],
    )
    filament_a = Filament.objects.create(
        type="PLA", brand="SUNLU", color="Black", color_hex="#000000",
        current_printer=session_a.printer, is_loaded_in_ams=True,
        current_tray_id=0, ams_unit_id=0, ams_type="AMS",
        remaining_percent=90,
    )
    filament_b = Filament.objects.create(
        type="PETG", brand="OVERTURE", color="White", color_hex="#FFFFFF",
        current_printer=session_b.printer, is_loaded_in_ams=True,
        current_tray_id=0, ams_unit_id=0, ams_type="AMS",
        remaining_percent=80,
    )

    cmd = Command()
    cmd.verbose = False
    cmd._collect_printer_data(session_a)
    cmd._collect_printer_data(session_b)

    snap_a = FilamentSnapshot.objects.get(printer_metric__device=session_a.printer)
    snap_b = FilamentSnapshot.objects.get(printer_metric__device=session_b.printer)
    assert snap_a.filament == filament_a
    assert snap_b.filament == filament_b
