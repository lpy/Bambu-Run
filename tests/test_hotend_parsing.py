from bambu_run.mqtt_client import BambuPrinter, PrinterState


def real_nozzle_payload():
    """Real captured device.nozzle payload from a live H2C with a Vortek rack
    (1x AMS, 1x AMS 2 Pro, 1x AMS HT physically connected — unrelated here).
    SN/used-time cross-checked against the user's Bambu Studio Hotends Info table."""
    return {
        "exist": 3997699,
        "src_id": 17,
        "tar_id": 17,
        "state": 0,
        "info": [
            {"id": 21, "sn": "20D06A5B2918952", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFA01", "color_m": "FFFFFFFF", "p_t": 11472, "wear": 128.0, "stat": 0, "tm": 350},
            {"id": 1, "sn": "N/A", "type": "HS01", "diameter": 0.4,
             "fila_id": "", "color_m": "00000000", "p_t": 0, "wear": 0.0, "stat": 0, "tm": 0},
            {"id": 16, "sn": "20D06A5B2919219", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFA01", "color_m": "A3D8E1FF", "p_t": 105386, "wear": 128.0, "stat": 0, "tm": 350},
            {"id": 20, "sn": "20D06A590610257", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFG01", "color_m": "00000000", "p_t": 81506, "wear": 128.0, "stat": 0, "tm": 350},
            {"id": 18, "sn": "20D06A591506263", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFA01", "color_m": "DE4343FF", "p_t": 30962, "wear": 128.0, "stat": 0, "tm": 350},
            {"id": 0, "sn": "20D06A5C0426280", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFA00", "color_m": "FEC600FF", "p_t": 93490, "wear": 128.0, "stat": 0, "tm": 350},
            {"id": 19, "sn": "20D06A5C0207881", "type": "HS01", "diameter": 0.4,
             "fila_id": "GFA01", "color_m": "DE4343FF", "p_t": 1430, "wear": 128.0, "stat": 0, "tm": 350},
        ],
    }


def make_data(nozzle_payload):
    return {"print": {"gcode_state": "IDLE", "device": {"nozzle": nozzle_payload}}}


def test_snapshot_includes_one_hotend_per_nozzle_info_entry():
    state = PrinterState.from_mqtt_data(make_data(real_nozzle_payload()))
    snapshot = state.get_snapshot()

    assert len(snapshot["hotends"]) == 7


def test_hotend_fields_extracted_correctly():
    state = PrinterState.from_mqtt_data(make_data(real_nozzle_payload()))
    snapshot = state.get_snapshot()

    by_sn = {h["serial_number"]: h for h in snapshot["hotends"]}
    h = by_sn["20D06A5B2919219"]

    assert h["raw_id"] == 16
    assert h["nozzle_type"] == "HS01"
    assert h["diameter"] == 0.4
    assert h["fila_id"] == "GFA01"
    assert h["color"] == "A3D8E1"  # alpha stripped
    assert h["used_time_seconds"] == 105386
    assert h["wear_percent"] == 100.0  # 128/128*100
    assert h["is_empty"] is False


def test_id_zero_is_toolhead_and_resolves_slot_number():
    state = PrinterState.from_mqtt_data(make_data(real_nozzle_payload()))
    snapshot = state.get_snapshot()

    by_sn = {h["serial_number"]: h for h in snapshot["hotends"]}
    toolhead = by_sn["20D06A5C0426280"]

    assert toolhead["raw_id"] == 0
    assert toolhead["is_toolhead"] is True
    assert toolhead["slot_number"] is None  # true bay address hidden while id==0 sentinel


def test_rack_bay_ids_resolve_to_slot_numbers_one_through_six():
    state = PrinterState.from_mqtt_data(make_data(real_nozzle_payload()))
    snapshot = state.get_snapshot()

    by_sn = {h["serial_number"]: h for h in snapshot["hotends"]}

    assert by_sn["20D06A5B2919219"]["slot_number"] == 1  # raw_id 16
    assert by_sn["20D06A591506263"]["slot_number"] == 3  # raw_id 18
    assert by_sn["20D06A5C0207881"]["slot_number"] == 4  # raw_id 19
    assert by_sn["20D06A590610257"]["slot_number"] == 5  # raw_id 20
    assert by_sn["20D06A5B2918952"]["slot_number"] == 6  # raw_id 21


def test_empty_bay_with_na_serial_is_flagged_empty():
    state = PrinterState.from_mqtt_data(make_data(real_nozzle_payload()))
    snapshot = state.get_snapshot()

    by_sn = {h["serial_number"]: h for h in snapshot["hotends"]}
    empty = by_sn["N/A"]

    assert empty["is_empty"] is True
    assert empty["is_toolhead"] is False


def test_snapshot_hotends_empty_list_when_no_nozzle_payload():
    state = PrinterState.from_mqtt_data({"print": {"gcode_state": "IDLE"}})
    snapshot = state.get_snapshot()

    assert snapshot["hotends"] == []


def test_empty_printer_accumulator_returns_no_snapshot():
    printer = BambuPrinter(token="token", device_id="SERIAL-A")

    assert printer.get_snapshot() is None


def test_absent_temperature_fields_remain_unknown_not_zero():
    state = PrinterState.from_mqtt_data({"print": {"gcode_state": "IDLE"}})
    snapshot = state.get_snapshot()

    assert snapshot["nozzle_temp"] is None
    assert snapshot["bed_temp"] is None
    assert snapshot["print_percent"] is None


def test_snapshot_keeps_ams_tray_with_display_info_even_without_type():
    state = PrinterState.from_mqtt_data({
        "print": {
            "gcode_state": "IDLE",
            "ams": {
                "ams": [{
                    "id": "0",
                    "info": "10001003",
                    "humidity": 5,
                    "temp": 22.5,
                    "tray": [{
                        "id": "1",
                        "tray_color": "FFFFFFFF",
                        "remain": -1,
                        "tray_info_idx": "GFS00",
                    }],
                }],
            },
        }
    })

    snapshot = state.get_snapshot()

    assert snapshot["ams_units"][0]["ams_type"] == "AMS 2 Pro"
    assert snapshot["filaments"] == [{
        "tray_id": "1",
        "slot": "",
        "type": "",
        "sub_type": "",
        "color": "FFFFFFFF",
        "remain_percent": -1,
        "tray_weight": 0,
        "tray_diameter": 1.75,
        "nozzle_temp_min": 0,
        "nozzle_temp_max": 0,
        "tag_uid": "",
        "state": 0,
        "tray_uuid": "",
        "k": 0.0,
        "n": 0.0,
        "cali_idx": -1,
        "total_len": 0,
        "tray_info_idx": "GFS00",
        "tray_time": 0,
        "tray_bed_temp": 0,
        "bed_temp_type": 0,
        "cols": [],
        "ams_unit_id": 0,
        "ams_info": "10001003",
        "ams_type": "AMS 2 Pro",
    }]


def test_snapshot_includes_external_spool_virtual_tray_fields():
    state = PrinterState.from_mqtt_data({
        "print": {
            "gcode_state": "IDLE",
            "vt_tray": {
                "id": "254",
                "tray_type": "PETG",
                "tray_sub_brands": "PETG Basic",
                "tray_color": "00FF00FF",
                "remain": 0,
                "tray_diameter": "1.75",
                "tray_weight": "1000",
                "tag_uid": "TAG-EXTERNAL",
                "tray_uuid": "UUID-EXTERNAL",
                "tray_info_idx": "GFG00",
            },
        }
    })

    snapshot = state.get_snapshot()

    assert snapshot["external_spool"]["tray_id"] == "254"
    assert snapshot["external_spool"]["type"] == "PETG"
    assert snapshot["external_spool"]["sub_type"] == "PETG Basic"
    assert snapshot["external_spool"]["color"] == "00FF00FF"
    assert snapshot["external_spool"]["remain_percent"] == 0
    assert snapshot["external_spool"]["tray_weight"] == "1000"
    assert snapshot["external_spool"]["is_external"] is True
