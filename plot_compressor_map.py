import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np


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
        help="Blade count used to convert single-passage mass flow into total flow.",
    )
    parser.add_argument(
        "--crop-gap-gs",
        type=float,
        default=0.20,
        help="Trim disconnected low-flow clusters when the mass-flow gap exceeds this value in g/s.",
    )
    parser.add_argument(
        "--fit-method",
        default="auto",
        help="Surge-fit method. Use 'auto' to compare candidates and choose the best one.",
    )
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
    return float(text)


def load_rows(csv_path: Path, blade_count: float):
    rows = []
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [str(name).strip() for name in (reader.fieldnames or [])]
        required = {"Result_File", "Mass_Flow_kg_s", "Static_PR"}
        missing = required.difference(fieldnames)
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")

        for raw_row in reader:
            row = sanitize_row(raw_row)
            result_file = str(row.get("Result_File", "")).strip()
            match = RUN_NAME_PATTERN.search(result_file)
            if not match:
                continue

            mass_flow_kg = get_float(row, "Mass_Flow_kg_s")
            pr = get_float(row, "Static_PR")
            efficiency = get_float(row, "Isentropic_Efficiency")
            if mass_flow_kg is None or pr is None or mass_flow_kg <= 0 or pr <= 0:
                continue

            rows.append(
                {
                    "result_file": result_file,
                    "speed": int(match.group("speed")),
                    "target_pressure": float(match.group("pressure")),
                    "mass_flow_gs": mass_flow_kg * 1000.0 * blade_count,
                    "pressure_ratio": pr,
                    "efficiency_pct": (
                        efficiency * 100.0 if efficiency is not None and efficiency <= 1.0 else efficiency
                    ),
                }
            )
    return rows


def interpolate_efficiency(primary_rows, efficiency_rows):
    direct_lookup = {
        row["result_file"]: row["efficiency_pct"]
        for row in efficiency_rows
        if row.get("efficiency_pct") is not None
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

        direct = direct_lookup.get(row["result_file"])
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
        "poly_deg_3",
        "poly_deg_4",
        "cubic_spline_natural",
        "pchip",
        "univariate_spline_s_0.0005",
        "univariate_spline_s_0.001",
        "univariate_spline_s_0.005",
    ]

    comparison = []
    for name in candidate_names:
        result = build_candidate_result(name, x, y)
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


def fit_surge_curve(grouped_points, fit_method):
    surge_points = extract_surge_points(grouped_points)

    if len(surge_points) < 3:
        return surge_points, None, []

    x = np.array([point["mass_flow_gs"] for point in surge_points], dtype=float)
    y = np.array([point["pressure_ratio"] for point in surge_points], dtype=float)
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


def plot_map(grouped_points, output_path: Path, blade_count: float, surge_result):
    fig, ax = plt.subplots(figsize=(11.5, 7.2), constrained_layout=True)

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
            f"{speed}",
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
        ax.legend(loc="lower left", frameon=True, facecolor="white", edgecolor="#cccccc")

        annotation_text = build_annotation_text(surge_result)
        if annotation_text:
            ax.text(
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

    ax.set_title("Centrifugal Compressor Performance Map", fontsize=19, fontweight="bold", pad=10)
    ax.set_xlabel("Mass Flow(g/s)", fontsize=13)
    ax.set_ylabel("Static Pressure Ratio", fontsize=13)
    ax.grid(True, linestyle="--", linewidth=0.8, alpha=0.3)
    ax.margins(x=0.03, y=0.05)

    fig.savefig(output_path, dpi=300)


def main():
    args = parse_args()
    input_path = Path(args.input)
    if not input_path.exists():
        raise FileNotFoundError(f"Input CSV not found: {input_path}")

    rows = load_rows(input_path, blade_count=args.blade_count)
    if not rows:
        raise RuntimeError("No valid compressor-map rows found in the primary CSV.")

    efficiency_path = Path(args.efficiency_input)
    if efficiency_path.exists():
        efficiency_rows = load_rows(efficiency_path, blade_count=args.blade_count)
        rows = interpolate_efficiency(rows, efficiency_rows)

    grouped_points = prepare_speed_lines(rows, crop_gap_gs=args.crop_gap_gs)
    surge_points, surge_result, comparison = fit_surge_curve(grouped_points, args.fit_method)
    plot_map(grouped_points, Path(args.output), blade_count=args.blade_count, surge_result=surge_result)

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
        else:
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
