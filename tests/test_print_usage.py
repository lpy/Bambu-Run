from bambu_run.print_usage import grams_from_length, parse_gcode_text


def test_parse_filament_used_grams_comment():
    parsed = parse_gcode_text("; filament used [g] = 12.5, 7.5\n")

    assert parsed.per_tool_grams == [12.5, 7.5]
    assert parsed.total_grams == 20.0


def test_parse_keeps_line_usage_with_filament_used_comment():
    parsed = parse_gcode_text(
        "\n".join([
            "; filament used [g] = 200.0",
            "M82",
            "G1 X0 E25.0",
            "G1 X1 E60.0",
            "G1 X2 E100.0",
        ])
    )

    assert parsed.total_grams == 200.0
    assert parsed.total_mm == 100.0
    assert parsed.mm_until_line(4) == 60.0


def test_parse_extrusion_moves_by_tool():
    parsed = parse_gcode_text(
        "\n".join([
            "M82",
            "T0",
            "G1 X0 E1.0",
            "G1 X1 E2.5",
            "G1 X2 E2.0 ; retract ignored",
            "T1",
            "G92 E0",
            "G1 X0 E4.0",
        ])
    )

    assert parsed.per_tool_mm == [2.5, 4.0]
    assert parsed.total_mm == 6.5


def test_grams_from_length_uses_material_density_and_diameter():
    grams = grams_from_length(1000, diameter_mm=1.75, material_type="PLA")

    assert round(grams, 2) == 2.98
