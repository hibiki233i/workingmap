import argparse
import csv
import math
import os
import re
import tempfile
import warnings
from collections import defaultdict
from itertools import cycle
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", str(Path(tempfile.gettempdir()) / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(Path(tempfile.gettempdir()) / "cache"))

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


# Journal figure conventions: serif text with matching math, inward ticks on all
# four sides, thin lines, open markers, TrueType fonts embedded in vector output.
PAPER_STYLE = {
    "font.family": "serif",
    "font.serif": ["Times New Roman", "Times", "Nimbus Roman", "STIXGeneral", "DejaVu Serif"],
    "mathtext.fontset": "stix",
    "font.size": 9,
    "axes.labelsize": 9,
    "axes.titlesize": 9,
    "axes.linewidth": 0.8,
    "legend.fontsize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "xtick.direction": "in",
    "ytick.direction": "in",
    "xtick.top": True,
    "ytick.right": True,
    "xtick.minor.visible": True,
    "ytick.minor.visible": True,
    "xtick.major.size": 3.5,
    "ytick.major.size": 3.5,
    "xtick.minor.size": 2.0,
    "ytick.minor.size": 2.0,
    "xtick.major.width": 0.8,
    "ytick.major.width": 0.8,
    "xtick.minor.width": 0.6,
    "ytick.minor.width": 0.6,
    "lines.linewidth": 1.0,
    "lines.markersize": 3.8,
    "savefig.dpi": 600,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    "svg.fonttype": "none",
}
PAPER_MARKERS = ("o", "s", "^", "v", "D", "<", ">", "p", "h")
PAPER_WIDTH_IN = 7.0  # double-column width
PAPER_HEIGHT_IN = 3.4

RUN_NAME_PATTERN = re.compile(r"Map_Speed_(?P<speed>\d+)_Press_(?P<pressure>[-+]?\d+(?:\.\d+)?)")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Plot a compressor map with automatic point trimming and surge-line fitting."
    )
    parser.add_argument(
        "--input",
        default="Compressor_Map_Data.csv",
        help="Primary compressor-map CSV.",
    )
    parser.add_argument(
        "--efficiency-input",
        default="Extracted_Compressor_Data.csv",
        help="Optional CSV used to provide or interpolate isentropic efficiency.",
    )
    parser.add_argument(
        "--output",
        default="Compressor_Map_With_Efficiency.png",
        help="Output PNG path.",
    )
    parser.add_argument(
        "--blade-count",
        type=float,
        default=10.0,
        help=(
            "Blade count for plotting. CSV rows with Blade_Count are rescaled from the recorded "
            "blade count; legacy rows without Blade_Count are treated as single-passage flow."
        ),
    )
    parser.add_argument(
        "--crop-gap-gs",
        type=float,
        default=0.20,
        help="Trim disconnected low-flow clusters when the mass-flow gap exceeds this value in g/s.",
    )
    parser.add_argument(
        "--fit-method",
        default="poly_deg_2",
        help="Surge-fit method (poly_deg_1, poly_deg_2, or auto).",
    )
    parser.add_argument("--pr-type", choices=("static", "total"), default="static",
                        help="Pressure ratio to plot (default: static, for legacy compatibility).")
    parser.add_argument("--design-point", help="Optional one-row map CSV marking the design point.")
    parser.add_argument("--style", choices=("map", "paper"), default="map",
                        help="map: efficiency-coloured performance map (default); paper: journal-style figure with "
                             "(a) pressure ratio and (b) isentropic efficiency against mass flow. The output "
                             "extension selects the format (.png at 600 dpi, .pdf/.svg as vector).")
    parser.add_argument("--design-label", default="",
                        help="Where the design point comes from, shown in its annotation (e.g. the optimization row).")
    return parser.parse_args()


def sanitize_row(raw_row):
    return {str(key).strip(): value for key, value in raw_row.items()}


def get_float(row, key):
    value = row.get(key)
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def scale_mass_flow_to_blade_count(row, mass_flow_kg, blade_count):
    recorded_blade_count = get_float(row, "Blade_Count")
    if recorded_blade_count is None:
        recorded_blade_count = 1.0
    if recorded_blade_count <= 0:
        raise ValueError(f"Blade_Count must be greater than 0 in row: {row}")
    return mass_flow_kg * blade_count / recorded_blade_count


def load_rows(csv_path: Path, blade_count: float, pr_type="static"):
    if not math.isfinite(blade_count) or blade_count <= 0:
        raise ValueError("Plot blade count must be finite and greater than 0")
    if pr_type not in ("static", "total"):
        raise ValueError(f"Unsupported pressure ratio type: {pr_type}")
    pr_column = "Total_PR" if pr_type == "total" else "Static_PR"
    rows = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(name).strip() for name in (reader.fieldnames or [])]
        missing = {"Mass_Flow_kg_s", pr_column}.difference(fieldnames)
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")

        for line_number, raw_row in enumerate(reader, start=2):
            row = sanitize_row(raw_row)
            context = f"{csv_path}:{line_number}"
            result_file = str(row.get("Result_File", "")).strip()
            version = str(row.get("schema_version", "")).strip()
            if version not in ("", "1"):
                raise ValueError(f"{context}: unsupported schema_version {version!r}")
            metadata = {}
            invalid = False
            for key, group in (("Speed_RPM", "speed"), ("Back_Pressure_Pa", "pressure")):
                value = get_float(row, key)
                if not str(row.get(key, "")).strip():
                    match = RUN_NAME_PATTERN.search(result_file)
                    if match:
                        value = float(match.group(group))
                        warnings.warn(f"{context}: {key} missing; using legacy filename metadata")
                if value is None or (key == "Speed_RPM" and value <= 0):
                    warnings.warn(f"{context}: invalid or missing {key}; skipping row")
                    invalid = True
                metadata[key] = value
            if invalid:
                continue

            mass_flow_kg = get_float(row, "Mass_Flow_kg_s")
            pr = get_float(row, pr_column)
            efficiency = get_float(row, "Isentropic_Efficiency")
            if mass_flow_kg is None or pr is None or mass_flow_kg <= 0 or pr <= 0:
                warnings.warn(f"{context}: invalid Mass_Flow_kg_s or {pr_column}; skipping row")
                continue
            recorded_count = get_float(row, "Blade_Count")
            if not str(row.get("Blade_Count", "")).strip():
                warnings.warn(f"{context}: Blade_Count missing; assuming single-passage flow")
            elif recorded_count is None or recorded_count <= 0:
                warnings.warn(f"{context}: invalid Blade_Count; skipping row")
                continue

            mass_flow_kg = scale_mass_flow_to_blade_count(row, mass_flow_kg, blade_count)
            rows.append({
                "result_file": result_file,
                "speed": metadata["Speed_RPM"],
                "target_pressure": metadata["Back_Pressure_Pa"],
                "mass_flow_gs": mass_flow_kg * 1000.0,
                "pressure_ratio": pr,
                "efficiency_pct": (
                    efficiency * 100.0 if efficiency is not None and efficiency <= 1.0 else efficiency
                ),
            })
    return rows


def interpolate_efficiency(primary_rows, efficiency_rows):
    direct_lookup = {
        (row["result_file"], row["speed"], row["target_pressure"]): row["efficiency_pct"]
        for row in efficiency_rows
        if row["result_file"] and row.get("efficiency_pct") is not None
    }
    by_speed = defaultdict(list)
    for row in efficiency_rows:
        eff = row.get("efficiency_pct")
        if eff is None:
            continue
        by_speed[row["speed"]].append((row["pressure_ratio"], eff))

    for speed in by_speed:
        by_speed[speed].sort(key=lambda item: item[0])

    for row in primary_rows:
        if row.get("efficiency_pct") is not None:
            continue

        direct = direct_lookup.get((row["result_file"], row["speed"], row["target_pressure"]))
        if direct is not None:
            row["efficiency_pct"] = direct
            continue

        refs = by_speed.get(row["speed"], [])
        if len(refs) == 1:
            row["efficiency_pct"] = refs[0][1]
        elif len(refs) >= 2:
            pr_ref = np.array([item[0] for item in refs], dtype=float)
            eff_ref = np.array([item[1] for item in refs], dtype=float)
            row["efficiency_pct"] = float(np.interp(row["pressure_ratio"], pr_ref, eff_ref))

    return primary_rows


def trim_disconnected_clusters(points, crop_gap_gs):
    if len(points) < 4:
        return points

    points = sorted(points, key=lambda item: item["mass_flow_gs"])
    clusters = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        if current["mass_flow_gs"] - previous["mass_flow_gs"] > crop_gap_gs:
            clusters.append([current])
        else:
            clusters[-1].append(current)

    return max(
        clusters,
        key=lambda cluster: (len(cluster), np.mean([point["mass_flow_gs"] for point in cluster])),
    )


def thin_surge_side_points(points, speed, speed_min, speed_max):
    if len(points) < 8:
        return points

    points = list(points)
    speed_ratio = 0.0 if speed_max <= speed_min else (speed - speed_min) / (speed_max - speed_min)
    min_flow_gap = 0.02 + 0.045 * speed_ratio
    min_pr_gap = 0.018 + 0.022 * speed_ratio
    surge_fraction = 0.35 + 0.18 * speed_ratio

    pr_values = [point["pressure_ratio"] for point in points]
    pr_max = max(pr_values)
    pr_min = min(pr_values)
    if pr_max <= pr_min:
        return points

    surge_side_threshold = pr_max - surge_fraction * (pr_max - pr_min)
    kept = [points[0]]

    for point in points[1:-1]:
        previous = kept[-1]
        near_surge = point["pressure_ratio"] >= surge_side_threshold
        flow_gap = abs(point["mass_flow_gs"] - previous["mass_flow_gs"])
        pr_gap = abs(point["pressure_ratio"] - previous["pressure_ratio"])

        if (not near_surge) or flow_gap >= min_flow_gap or pr_gap >= min_pr_gap:
            kept.append(point)

    if points[-1] is not kept[-1]:
        kept.append(points[-1])
    return kept


def prepare_speed_lines(rows, crop_gap_gs):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["speed"]].append(row)

    speed_values = sorted(grouped)
    speed_min = min(speed_values) if speed_values else 0
    speed_max = max(speed_values) if speed_values else 0

    trimmed = {}
    for speed, points in grouped.items():
        trimmed[speed] = trim_disconnected_clusters(points, crop_gap_gs)
        trimmed[speed].sort(
            key=lambda item: (
                -item["target_pressure"],
                -item["pressure_ratio"],
                item["mass_flow_gs"],
            )
        )
        trimmed[speed] = thin_surge_side_points(trimmed[speed], speed, speed_min, speed_max)
    return trimmed


def extract_surge_points(grouped_points):
    surge_points = []
    for speed in sorted(grouped_points):
        points = grouped_points[speed]
        if points:
            surge_points.append(min(points, key=lambda item: item["mass_flow_gs"]))
    return surge_points


def build_poly_result(name, degree, x, y):
    coefficients = np.polyfit(x, y, degree)
    predictor = lambda xin, coefficients=coefficients: np.polyval(coefficients, xin)
    return {
        "name": name,
        "display_name": f"Polynomial degree {degree}",
        "kind": "polynomial",
        "degree": degree,
        "coefficients": coefficients,
        "predictor": predictor,
    }


def build_spline_result(name, display_name, x, y, smoothing):
    spline = None if smoothing is None else smoothing(x, y)
    return {
        "name": name,
        "display_name": display_name,
        "kind": "spline",
        "predictor": spline,
    }


def compute_r_squared(y_true, y_pred):
    residual_sum = float(np.sum((y_true - y_pred) ** 2))
    total_sum = float(np.sum((y_true - y_true.mean()) ** 2))
    return 1.0 if total_sum == 0 else 1.0 - residual_sum / total_sum


def evaluate_fit_result(result, x, y):
    predictor = result["predictor"]
    y_fit = np.asarray(predictor(x), dtype=float)
    result["train_rmse"] = float(np.sqrt(np.mean((y - y_fit) ** 2)))
    result["train_r2"] = compute_r_squared(y, y_fit)
    result["max_abs_err"] = float(np.max(np.abs(y - y_fit)))
    return result


def build_candidate_result(name, x, y):
    if name.startswith("poly_deg_"):
        degree = int(name.rsplit("_", 1)[-1])
        return build_poly_result(name, degree, x, y)
    if name == "cubic_spline_natural":
        return build_spline_result(
            name,
            "Natural cubic spline",
            x,
            y,
            lambda x_train, y_train: __import__("scipy.interpolate").interpolate.CubicSpline(
                x_train, y_train, bc_type="natural"
            ),
        )
    if name == "pchip":
        return build_spline_result(
            name,
            "PCHIP",
            x,
            y,
            lambda x_train, y_train: __import__("scipy.interpolate").interpolate.PchipInterpolator(x_train, y_train),
        )
    if name.startswith("univariate_spline_s_"):
        smoothing = float(name.split("_")[-1])
        return build_spline_result(
            name,
            f"Univariate spline (s={smoothing:g})",
            x,
            y,
            lambda x_train, y_train, smoothing=smoothing: __import__("scipy.interpolate").interpolate.UnivariateSpline(
                x_train, y_train, s=smoothing
            ),
        )
    raise ValueError(f"Unsupported fit method: {name}")


def compare_fit_methods(x, y):
    candidate_names = [
        "poly_deg_1",
        "poly_deg_2",
    ]

    comparison = []
    for name in candidate_names:
        try:
            result = build_candidate_result(name, x, y)
        except Exception:
            continue
        loo_errors = []
        failed = False
        for index in range(len(x)):
            mask = np.ones(len(x), dtype=bool)
            mask[index] = False
            try:
                partial = build_candidate_result(name, x[mask], y[mask])
                predictor = partial["predictor"]
                predicted = float(np.asarray(predictor(np.array([x[index]])), dtype=float)[0])
                if not np.isfinite(predicted):
                    failed = True
                    break
                loo_errors.append((predicted - y[index]) ** 2)
            except Exception:
                failed = True
                break

        if failed or not loo_errors:
            continue

        result = evaluate_fit_result(result, x, y)
        result["loo_rmse"] = float(np.sqrt(np.mean(loo_errors)))
        comparison.append(result)

    comparison.sort(key=lambda item: (item["loo_rmse"], -item["train_r2"]))
    return comparison


def prepare_fit_xy(points):
    by_flow = defaultdict(list)
    for point in points:
        by_flow[point["mass_flow_gs"]].append(point["pressure_ratio"])

    x_values = []
    y_values = []
    for flow in sorted(by_flow):
        x_values.append(flow)
        y_values.append(float(np.mean(by_flow[flow])))

    return np.array(x_values, dtype=float), np.array(y_values, dtype=float)


def fit_surge_curve(grouped_points, fit_method):
    surge_points = extract_surge_points(grouped_points)

    if len(surge_points) < 3:
        return surge_points, None, []

    x, y = prepare_fit_xy(surge_points)
    if len(x) < 3:
        return surge_points, None, []

    comparison = compare_fit_methods(x, y)
    if not comparison:
        return surge_points, None, []

    if fit_method == "auto":
        result = comparison[0]
    else:
        matches = [item for item in comparison if item["name"] == fit_method]
        if not matches:
            available = ", ".join(item["name"] for item in comparison)
            raise ValueError(f"Unknown or unsupported fit method '{fit_method}'. Available: {available}")
        result = matches[0]

    x_fit = np.linspace(x.min(), x.max(), 300)
    y_fit = np.asarray(result["predictor"](x_fit), dtype=float)
    result = dict(result)
    result["x_fit"] = x_fit
    result["y_fit"] = y_fit
    result["comparison"] = comparison
    result["best_polynomial"] = next((item for item in comparison if item["kind"] == "polynomial"), None)
    return surge_points, result, comparison


def enforce_surge_boundary(surge_result, all_points):
    """Shift the surge polynomial so every data point lies to the right/below the surge line.

    For a polynomial PR = f(flow), adds a vertical offset to the constant term so that
    f(flow_i) >= PR_i for all data points.  Returns a copy of surge_result (or None if
    the result is not a polynomial).
    """
    if surge_result is None or surge_result.get("kind") != "polynomial":
        return surge_result

    coeffs = surge_result["coefficients"]
    predictor = surge_result["predictor"]

    all_pr = np.array([p["pressure_ratio"] for p in all_points])
    pr_range = float(np.max(all_pr) - np.min(all_pr))

    max_violation = 0.0
    for point in all_points:
        flow = point["mass_flow_gs"]
        surge_pr = float(predictor(flow))
        violation = point["pressure_ratio"] - surge_pr
        if violation > max_violation:
            max_violation = violation

    if max_violation <= 0.0:
        return surge_result

    margin = 0.02 * pr_range
    offset = float(max_violation + margin)

    new_coeffs = list(coeffs)
    new_coeffs[-1] += offset

    new_predictor = lambda xin, c=new_coeffs: float(np.polyval(c, xin))

    result = dict(surge_result)
    result["coefficients"] = new_coeffs
    result["predictor"] = new_predictor
    result["surge_boundary_offset"] = offset

    flow_min = float(np.min([p["mass_flow_gs"] for p in all_points]))
    flow_max = float(np.max([p["mass_flow_gs"] for p in all_points]))
    x_fit = np.linspace(flow_min, flow_max, 300)
    y_fit = np.array([new_predictor(x) for x in x_fit], dtype=float)
    result["x_fit"] = x_fit
    result["y_fit"] = y_fit

    return result


def format_polynomial(coefficients):
    degree = len(coefficients) - 1
    terms = []
    for index, coefficient in enumerate(coefficients):
        power = degree - index
        sign = "-" if coefficient < 0 else "+"
        value = abs(float(coefficient))
        if power == 0:
            term_body = f"{value:.6f}"
        elif power == 1:
            term_body = f"{value:.6f} * x"
        else:
            term_body = f"{value:.6f} * x^{power}"

        if not terms:
            terms.append(f"-{term_body}" if coefficient < 0 else term_body)
        else:
            terms.append(f" {sign} {term_body}")
    return "".join(terms)


def build_annotation_text(surge_result):
    if surge_result is None:
        return None

    lines = [f"R^2 = {surge_result['train_r2']:.6f}"]
    if surge_result["kind"] == "polynomial":
        lines.insert(0, f"y = {format_polynomial(surge_result['coefficients'])}")
    else:
        best_polynomial = surge_result.get("best_polynomial")
        if best_polynomial is not None:
            lines.insert(0, f"y = {format_polynomial(best_polynomial['coefficients'])}")
        else:
            lines.insert(0, "Best fit is spline-based; no single global polynomial equation.")
    return "\n".join(lines)


def design_point_caption(point, pr_type, label=""):
    """Annotation text: heading with the source label, recorded performance, operating point."""
    values = [] if point.get("efficiency_pct") is None else [f"η {point['efficiency_pct']:.1f} %"]
    values += [f"{'Total' if pr_type == 'total' else 'Static'} PR {point['pressure_ratio']:.3f}",
               f"{point['mass_flow_gs']:.2f} g/s"]
    heading = "☆ Design point" + (f" · {label}" if label else "")
    return "\n".join([heading, "  ·  ".join(values),
                      f"{point['speed']:g} rpm, back pressure {point['target_pressure']:g} Pa"])


def plot_map(grouped_points, output_path: Path, blade_count: float, surge_result,
             pr_type="static", design_point=None, design_label=""):
    fig, ax = plt.subplots(figsize=(11.5, 7.2), constrained_layout=True)
    fit_box = None

    all_efficiencies = [
        point["efficiency_pct"]
        for points in grouped_points.values()
        for point in points
        if point.get("efficiency_pct") is not None
    ]
    color_norm = None
    color_map = "turbo"
    scatter_reference = None

    if all_efficiencies:
        color_norm = plt.Normalize(min(all_efficiencies), max(all_efficiencies))

    line_cmap = plt.cm.viridis
    speed_values = sorted(grouped_points)
    speed_norm = plt.Normalize(min(speed_values), max(speed_values)) if speed_values else None

    for speed in speed_values:
        points = grouped_points[speed]
        x = [point["mass_flow_gs"] for point in points]
        y = [point["pressure_ratio"] for point in points]
        line_color = line_cmap(speed_norm(speed)) if speed_norm is not None else "#1f77b4"

        ax.plot(x, y, color=line_color, linewidth=2.2, alpha=0.95, zorder=2)
        if color_norm is not None:
            scatter_reference = ax.scatter(
                x,
                y,
                c=[point["efficiency_pct"] for point in points],
                cmap=color_map,
                norm=color_norm,
                s=58,
                edgecolors="#202020",
                linewidths=0.5,
                zorder=3,
            )
        else:
            ax.scatter(
                x,
                y,
                color=line_color,
                s=58,
                edgecolors="#202020",
                linewidths=0.5,
                zorder=3,
            )

        ax.annotate(
            f"{speed:g}",
            xy=(x[-1], y[-1]),
            xytext=(6, 0),
            textcoords="offset points",
            fontsize=10,
            color="#202020",
            va="center",
            ha="left",
            bbox={"boxstyle": "round,pad=0.2", "fc": "white", "ec": "none", "alpha": 0.8},
        )

    surge_points = extract_surge_points(grouped_points)
    if surge_points:
        ax.scatter(
            [point["mass_flow_gs"] for point in surge_points],
            [point["pressure_ratio"] for point in surge_points],
            marker="D",
            s=54,
            color="#111111",
            edgecolors="white",
            linewidths=0.7,
            zorder=4,
            label="Surge points",
        )

    if surge_result is not None:
        ax.plot(
            surge_result["x_fit"],
            surge_result["y_fit"],
            color="#111111",
            linestyle="--",
            linewidth=2.0,
            zorder=1,
            label=f"Surge prediction ({surge_result['display_name']})",
        )

        annotation_text = build_annotation_text(surge_result)
        if annotation_text:
            fit_box = ax.text(
                0.02,
                0.98,
                annotation_text,
                transform=ax.transAxes,
                ha="left",
                va="top",
                fontsize=9.5,
                color="#111111",
                bbox={"boxstyle": "round,pad=0.35", "fc": "white", "ec": "#bbbbbb", "alpha": 0.92},
            )

    if scatter_reference is not None:
        cbar = fig.colorbar(scatter_reference, ax=ax, pad=0.02, shrink=0.98)
        cbar.set_label("Isentropic Efficiency (%)", fontsize=12, fontweight="bold")

    if design_point is not None:
        x, y = design_point["mass_flow_gs"], design_point["pressure_ratio"]
        ax.scatter([x], [y], marker="*", s=420, color="white", edgecolors="#111111", linewidths=1.4,
                   zorder=6, label="Design point (optimization record)")
        # Upper left lies beyond the surge line, where no map points fall; stack the
        # caption under the fit equation instead of drawing a leader across the lines.
        anchor = ({"xy": (0, 0), "xycoords": fit_box, "xytext": (0, -10), "textcoords": "offset points"}
                  if fit_box is not None else {"xy": (0.02, 0.98), "xycoords": "axes fraction"})
        ax.annotate(
            design_point_caption(design_point, pr_type, design_label),
            **anchor,
            ha="left",
            va="top",
            fontsize=9.5,
            color="#111111",
            bbox={"boxstyle": "round,pad=0.35", "fc": "white", "ec": "#bbbbbb", "alpha": 0.92},
            zorder=7,
        )

    if surge_points or design_point is not None:
        # Below the axes so it never hides the low-speed lines in the lower left.
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.09), ncol=3, frameon=False, fontsize=10.5)

    ax.set_title("Centrifugal Compressor Performance Map", fontsize=19, fontweight="bold", pad=10)
    ax.set_xlabel("Mass Flow (g/s)", fontsize=13)
    ax.set_ylabel("Total Pressure Ratio (stationary frame)" if pr_type == "total"
                  else "Static Pressure Ratio", fontsize=13)
    ax.grid(True, linestyle="--", linewidth=0.8, alpha=0.3)
    ax.margins(x=0.03, y=0.05)

    fig.savefig(output_path, dpi=300)
    plt.close(fig)


def pressure_ratio_label(pr_type):
    return (r"Total pressure ratio $\pi_\mathrm{t}$ (–)" if pr_type == "total"
            else r"Static pressure ratio $\pi_\mathrm{s}$ (–)")


def speed_line_xy(points, key):
    """One speed line as (mass flows, values) ordered by flow, skipping missing values."""
    pairs = sorted((point["mass_flow_gs"], point[key]) for point in points if point.get(key) is not None)
    return [flow for flow, _ in pairs], [value for _, value in pairs]


def surge_line_xy(surge_result, surge_points):
    """The fitted surge line over the flow range of the surge points it was fitted to."""
    flows = [point["mass_flow_gs"] for point in surge_points]
    x = np.linspace(min(flows), max(flows), 200)
    return x, np.array([float(surge_result["predictor"](value)) for value in x])


def design_point_note(point, pr_type, label=""):
    """Figure note with the optimization record of the design point."""
    symbol = r"\pi_\mathrm{t}" if pr_type == "total" else r"\pi_\mathrm{s}"
    values = [rf"$\dot{{m}}$ = {point['mass_flow_gs']:.2f} g/s", rf"${symbol}$ = {point['pressure_ratio']:.3f}"]
    if point.get("efficiency_pct") is not None:
        values.append(rf"$\eta_\mathrm{{is}}$ = {point['efficiency_pct']:.1f} %")
    values.append(f"{point['speed']:g} rpm, back pressure {point['target_pressure']:g} Pa")
    note = "Design point (optimization record): " + ", ".join(values)
    return f"{note}\nSource: {label}" if label else note


def plot_paper(grouped_points, output_path: Path, surge_result, pr_type="static", design_point=None,
               design_label=""):
    """(a) pressure ratio and (b) isentropic efficiency against mass flow, one curve per speed line."""
    with plt.rc_context(PAPER_STYLE):
        fig = plt.figure(figsize=(PAPER_WIDTH_IN, PAPER_HEIGHT_IN + (0.3 if design_point else 0.0)),
                         constrained_layout=True)
        rows = (0.17, 1.0, 0.11) if design_point else (0.17, 1.0)
        grid = fig.add_gridspec(len(rows), 2, height_ratios=rows)
        legend_ax = fig.add_subplot(grid[0, :])
        ax_pr = fig.add_subplot(grid[1, 0])
        ax_eff = fig.add_subplot(grid[1, 1], sharex=ax_pr)

        speeds = sorted(grouped_points)
        colors = plt.cm.viridis(np.linspace(0.0, 0.85, len(speeds))) if len(speeds) > 1 else ["#1b3a5c"]
        handles = []
        for speed, color, marker in zip(speeds, colors, cycle(PAPER_MARKERS)):
            style = {"color": color, "marker": marker, "markerfacecolor": "white",
                     "markeredgecolor": color, "markeredgewidth": 0.8}
            handles.append(ax_pr.plot(*speed_line_xy(grouped_points[speed], "pressure_ratio"),
                                      label=f"{speed:g} rpm", **style)[0])
            flows, efficiencies = speed_line_xy(grouped_points[speed], "efficiency_pct")
            if flows:
                ax_eff.plot(flows, efficiencies, **style)

        surge_points = extract_surge_points(grouped_points)
        if surge_result is not None and len(surge_points) >= 2:
            handles.append(ax_pr.plot(*surge_line_xy(surge_result, surge_points), color="black",
                                      linestyle="--", linewidth=1.0, label="Surge line", zorder=1)[0])

        if design_point is not None:
            star = {"marker": "*", "markersize": 10, "color": "black", "markeredgecolor": "white",
                    "markeredgewidth": 0.4, "linestyle": "none", "zorder": 6}
            flow = design_point["mass_flow_gs"]
            handles.append(ax_pr.plot([flow], [design_point["pressure_ratio"]], label="Design point", **star)[0])
            if design_point.get("efficiency_pct") is not None:
                ax_eff.plot([flow], [design_point["efficiency_pct"]], **star)
            note_ax = fig.add_subplot(grid[2, :])
            note_ax.axis("off")
            note_ax.text(0.0, 1.0, design_point_note(design_point, pr_type, design_label), transform=note_ax.transAxes,
                         ha="left", va="top", fontsize=7.5, color="#333333")

        if not ax_eff.lines:
            ax_eff.text(0.5, 0.5, "No efficiency data", transform=ax_eff.transAxes, ha="center", va="center")
        legend_ax.axis("off")
        legend_ax.legend(handles=handles, loc="center", ncol=min(6, len(handles)), frameon=False,
                         handlelength=2.4, columnspacing=1.4)
        for ax, panel, ylabel in ((ax_pr, "(a)", pressure_ratio_label(pr_type)),
                                  (ax_eff, "(b)", r"Isentropic efficiency $\eta_\mathrm{is}$ (%)")):
            ax.set_title(panel, loc="left")
            ax.set_xlabel(r"Mass flow rate $\dot{m}$ (g/s)")
            ax.set_ylabel(ylabel)
            ax.margins(x=0.04, y=0.06)
        fig.savefig(output_path)
        plt.close(fig)


def main():
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    rows = load_rows(input_path, blade_count=args.blade_count, pr_type=args.pr_type)
    if not rows:
        raise RuntimeError("No valid compressor-map rows found in the primary CSV.")

    efficiency_path = Path(args.efficiency_input) if args.efficiency_input else None
    if efficiency_path is not None and efficiency_path.is_file():
        try:
            efficiency_rows = load_rows(efficiency_path, blade_count=args.blade_count, pr_type=args.pr_type)
        except ValueError as exc:
            # Optional input: an older table (e.g. without Total_PR) must not block the map.
            warnings.warn(f"Ignoring efficiency CSV {efficiency_path}: {exc}")
        else:
            rows = interpolate_efficiency(rows, efficiency_rows)

    design_point = None
    if args.design_point:
        design_rows = load_rows(Path(args.design_point), args.blade_count, args.pr_type)
        if len(design_rows) != 1:
            raise ValueError("Design-point CSV must contain exactly one valid row")
        design_point = design_rows[0]

    grouped_points = prepare_speed_lines(rows, crop_gap_gs=args.crop_gap_gs)
    surge_points, surge_result, comparison = fit_surge_curve(grouped_points, args.fit_method)
    all_filtered = [p for pts in grouped_points.values() for p in pts]
    surge_result = enforce_surge_boundary(surge_result, all_filtered)
    if args.style == "paper":
        plot_paper(grouped_points, Path(args.output), surge_result=surge_result, pr_type=args.pr_type,
                   design_point=design_point, design_label=args.design_label)
    else:
        plot_map(grouped_points, Path(args.output), blade_count=args.blade_count, surge_result=surge_result,
                 pr_type=args.pr_type, design_point=design_point, design_label=args.design_label)

    if comparison:
        print("Surge fit comparison")
        for item in comparison:
            print(
                f"  {item['name']}: loo_rmse={item['loo_rmse']:.6f}, "
                f"train_r2={item['train_r2']:.6f}, train_rmse={item['train_rmse']:.6f}"
            )

    if surge_result is not None:
        print("Best surge prediction model")
        print(f"  Method: {surge_result['display_name']} ({surge_result['name']})")
        print(f"  LOO RMSE: {surge_result['loo_rmse']:.6f}")
        print(f"  R^2: {surge_result['train_r2']:.6f}")
        if surge_result["kind"] == "polynomial":
            print(f"  Equation: y = {format_polynomial(surge_result['coefficients'])}")
        if surge_result.get("surge_boundary_offset"):
            print(f"  Boundary offset applied: {surge_result['surge_boundary_offset']:.6f} (surge line now outside all points)")
        if surge_result["kind"] != "polynomial":
            best_polynomial = next((item for item in comparison if item["kind"] == "polynomial"), None)
            print("  Equation: no single closed-form global polynomial; this is a spline-based fit.")
            if best_polynomial is not None:
                print(
                    "  Best polynomial alternative: "
                    f"{best_polynomial['display_name']} with y = {format_polynomial(best_polynomial['coefficients'])}"
                )
                print(f"  Best polynomial R^2: {best_polynomial['train_r2']:.6f}")
    elif surge_points:
        print("Surge prediction curve")
        print("  Not enough surge points to fit a curve.")

    print(f"Saved plot to: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()
