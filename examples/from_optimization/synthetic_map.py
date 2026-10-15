"""Analytic stand-in for a cfx.ps1 sweep, anchored on the design point.

Each speed line starts at the scan table's initial back pressure and climbs
towards surge; flow falls and pressure ratio rises along the line. The design
speed line passes exactly through the design point at its back pressure, so the
plotted star must sit on that line. Values are SYNTHETIC, not CFD results.
"""
import csv
from pathlib import Path

# Position of the design back pressure along the design speed line
# (0 = initial back pressure, 1 = last point before surge).
DESIGN_FRACTION = 0.7
POINTS_PER_LINE = 12


def flow_shape(x):
    return 1.10 - 0.28 * x ** 1.6


def ratio_shape(x):
    return 0.72 + 0.38 * x - 0.10 * x ** 2


def read_csv(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return reader.fieldnames, list(reader)


def synthetic_rows(scan_rows, design):
    design_rpm = float(design["Speed_RPM"])
    design_pressure = float(design["Back_Pressure_Pa"])
    flow = float(design["Mass_Flow_kg_s"])
    total = float(design["Total_PR"])
    static = float(design["Static_PR"]) if design.get("Static_PR") else None
    efficiency = float(design["Isentropic_Efficiency"])
    rows = []
    for scan in scan_rows:
        speed = int(scan["SpeedRPM"])
        initial = float(scan["InitialPressurePa"])
        if design_pressure <= initial:
            raise ValueError("The synthetic map needs the design back pressure above the initial back pressure")
        ratio = speed / design_rpm
        end = initial + (design_pressure - initial) / DESIGN_FRACTION * ratio ** 2
        fractions = {index / (POINTS_PER_LINE - 1) for index in range(POINTS_PER_LINE)}
        if speed == round(design_rpm):
            fractions.add(DESIGN_FRACTION)
        for x in sorted(fractions):
            pressure = initial + x * (end - initial) if x != DESIGN_FRACTION else design_pressure
            pr_scale = ratio ** 2 * ratio_shape(x) / ratio_shape(DESIGN_FRACTION)
            rows.append({
                "schema_version": "1",
                "Result_File": f"SYNTHETIC_Map_Speed_{speed}_Press_{pressure:.4f}.res",
                "Speed_RPM": str(speed),
                "Back_Pressure_Pa": repr(round(pressure, 6)),
                "Mass_Flow_kg_s": repr(flow * ratio * flow_shape(x) / flow_shape(DESIGN_FRACTION)),
                "Static_PR": "" if static is None else repr(1 + (static - 1) * pr_scale),
                "Total_PR": repr(1 + (total - 1) * pr_scale),
                "Isentropic_Efficiency": repr(efficiency - 0.06 * (ratio - 1) ** 2 - 0.10 * (x - DESIGN_FRACTION) ** 2),
                "Blade_Count": design["Blade_Count"],
                "Mass_Flow_Unit": design["Mass_Flow_Unit"],
            })
    return rows


def write_synthetic_map(scan_points_csv, design_point_csv, output_csv):
    _, scan_rows = read_csv(scan_points_csv)
    fields, design_rows = read_csv(design_point_csv)
    if len(design_rows) != 1:
        raise ValueError(f"{design_point_csv} must hold exactly one design point")
    rows = synthetic_rows(scan_rows, design_rows[0])
    with Path(output_csv).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, restval="", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)
