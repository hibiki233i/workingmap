"""Prepare a workingmap sweep from one selected optimization result.

The impeller (cryogenic-compressor-impller-optimization) and blade
(compressor_blade_optimizition) optimizers record every CFD sample in its own
run directory. This tool takes one row of their Pareto CSV, finds the run
directory that produced it and writes what cfx.ps1 and plot_compressor_map.py
need, without running any CFD:

  scan_points.csv   speed lines around the design speed       -> cfx.ps1 -SpeedPressureTablePath
  base_map.ccl      MySpeed / MyBackPressure expressions      -> cfx.ps1 -BaseCclFile
  design_point.csv  the optimization record as a schema-v1 row -> plot --design-point
  manifest.json     provenance (repository, commit, CSV line, design vector,
                    performance, artifacts) and the follow-up commands

See docs/from_optimization.md.
"""
import argparse
import csv
import datetime
import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MANIFEST_VERSION = 1
ROOT = Path(__file__).resolve().parent

# cfx.ps1 stops a speed line once the back pressure exceeds $maxSafePressure.
MAX_SAFE_PRESSURE_PA = 16.0
DEFAULT_DESIGN_RPM = 10000.0
DEFAULT_INITIAL_PRESSURE_PA = 5.0

# CSV floats and run-directory JSON come from the same Python values; the
# tolerance only absorbs text round-tripping.
VECTOR_ATOL = 1e-6
VECTOR_RTOL = 1e-6
PERFORMANCE_RTOL = 1e-5

# Defaults mirror design_variables.DEFAULT_VARIABLE_SPECS (impeller) and the
# "variables" list of blade_shape_config.json (blade); --design-variables overrides.
SOURCE_VARIABLES = {
    "impeller": ("d1s", "dH", "beta1hb", "beta1sb", "d2", "b2", "beta2hb", "beta2sb",
                 "Lz", "t", "TipClear", "nBl", "rake_te_s"),
    "blade": tuple(f"{side}_beta_{index}_deg_offset" for side in ("hub", "shroud") for index in range(5))
             + ("hub_theta_deg_offset", "shroud_theta_deg_offset"),
}
# Variables the geometry and CFX runners execute as int(round(value)).
INTEGER_VARIABLES = {"nBl"}

# Optimization CSV columns: MassFlow is the full impeller in g/s, PressureRatio
# is static-to-static, totalpressureratio is stationary-frame total-to-total.
PERFORMANCE_COLUMNS = ("Efficiency", "PressureRatio", "totalpressureratio", "MassFlow", "Power")
REQUIRED_PERFORMANCE = ("Efficiency", "totalpressureratio", "MassFlow")
CFX_RESULT_FIELDS = ("Efficiency", "PressureRatio", "Power", "MassFlow", "totalpressureratio")

MAP_COLUMNS = ("schema_version", "Result_File", "Speed_RPM", "Back_Pressure_Pa", "Mass_Flow_kg_s",
               "Static_PR", "Total_PR", "P_in_Pa", "P_out_Pa", "T_in_K", "T_out_K",
               "Isentropic_Efficiency", "Blade_Count", "Mass_Flow_Unit")


class ConversionError(Exception):
    """The selected result cannot be turned into a sweep; the message says why."""


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Turn one optimization Pareto row into workingmap sweep inputs and a provenance manifest."
    )
    parser.add_argument("--source", choices=sorted(SOURCE_VARIABLES), required=True,
                        help="Which optimizer wrote the results CSV.")
    parser.add_argument("--results", type=Path, required=True,
                        help="Impeller pareto_engineering_ranked.csv / pareto_front_points.csv / training CSV, "
                             "or blade pareto_front.csv / training_data.csv.")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--select", metavar="COLUMN=VALUE",
                           help="Pick the single row whose COLUMN equals VALUE, e.g. engineering_rank=1 or run_id=case_000004.")
    selection.add_argument("--row", type=int, help="Pick the N-th data row (1-based, header excluded).")
    parser.add_argument("--run-dir", type=Path, help="Run directory of the selected row (skips the search).")
    parser.add_argument("--runs-root", type=Path, action="append", default=[],
                        help="Directory holding run directories (Runs/, ActiveLearning_Runs/, or a blade output "
                             "directory with cases/). Repeatable.")
    parser.add_argument("--audit-csv", type=Path,
                        help="Impeller failure_records.csv; supplies design vectors for run directories "
                             "without input_parameters.json.")
    parser.add_argument("--design-variables", type=Path,
                        help='JSON with {"variables": [{"name": ...}, ...]} (impeller design_variables.json or '
                             "blade config); default: the optimizer's built-in variable list.")
    parser.add_argument("--output-dir", type=Path, required=True, help="New or empty directory for the outputs.")
    parser.add_argument("--design-rpm", type=float, help=f"Design speed, rpm (default {DEFAULT_DESIGN_RPM:g}).")
    parser.add_argument("--design-p-out-pa", type=float,
                        help="Design back pressure, Pa (default: the row's P_out or the blade case's cfx_state.json).")
    parser.add_argument("--passage-count", type=int,
                        help="Passages represented by MassFlow (default: nBl for the impeller, n_blades from "
                             "the blade case's cfx_state.json).")
    parser.add_argument("--speed-span", type=float, default=0.20,
                        help="Speed lines cover design speed x (1 +/- span) (default 0.20).")
    parser.add_argument("--speed-step", type=float, default=0.05,
                        help="Spacing of speed lines as a fraction of design speed (default 0.05).")
    parser.add_argument("--speeds-rpm", help="Explicit comma-separated speed lines, rpm; overrides span/step.")
    parser.add_argument("--initial-pressure-pa", type=float, default=DEFAULT_INITIAL_PRESSURE_PA,
                        help=f"Starting back pressure of every speed line, Pa (default {DEFAULT_INITIAL_PRESSURE_PA:g}).")
    parser.add_argument("--pr-type", choices=("static", "total"), default="static",
                        help="Pressure ratio for the plot command (default static; total is the optimization objective).")
    parser.add_argument("--plot-style", choices=("paper", "map"), default="paper",
                        help="Plot command style: paper (pressure ratio and efficiency against mass flow, default) "
                             "or map (efficiency-coloured performance map).")
    parser.add_argument("--mass-flow-unit", choices=("kg/s", "g/s"), default="g/s",
                        help="CFD-Post mass-flow unit to pass to cfx.ps1 (default g/s, the optimizers' CFX unit system).")
    parser.add_argument("--def-ccl", type=Path,
                        help="CCL text of the solver input (cfx5cmds -read -def <file> -text <ccl>); checks that "
                             "MySpeed and MyBackPressure actually drive the domain speed and outlet.")
    parser.add_argument("--bind-speed-domain", metavar="DOMAIN",
                        help="Also write 'Angular Velocity = MySpeed' for this rotating domain into base_map.ccl.")
    parser.add_argument("--flow-analysis", default="Flow Analysis 1",
                        help="FLOW name used by --bind-speed-domain (default 'Flow Analysis 1').")
    parser.add_argument("--source-repo",
                        help="Optimizer repository (local checkout or URL) that produced the results.")
    parser.add_argument("--source-commit", help="Commit of the optimizer that produced the results.")
    parser.add_argument("--data-source", default="unspecified",
                        help="What the results are, e.g. cfd or synthetic_analytic_v1; "
                             "values starting with 'synthetic' label the plot.")
    return parser.parse_args(argv)


# --------------------------------------------------------------------------- inputs

def finite(value, context):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        raise ConversionError(f"{context}: {value!r} is not a number") from None
    if not math.isfinite(number):
        raise ConversionError(f"{context}: {value!r} is not finite")
    return number


def optional_finite(row, key):
    text = str(row.get(key, "") or "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def read_results(path):
    if not path.is_file():
        raise ConversionError(f"Results CSV not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = [str(name).strip() for name in (reader.fieldnames or [])]
        rows = []
        for raw in reader:
            row = {str(key).strip(): (value or "").strip() for key, value in raw.items() if key is not None}
            rows.append((reader.line_num, row))
    if not rows:
        raise ConversionError(f"{path}: no data rows")
    return fields, rows


def check_columns(fields, source, variables, path):
    if "totalpressureratio" not in fields and any(name.startswith("pred_") for name in fields):
        raise ConversionError(
            f"{path} holds surrogate predictions (pred_*), which have no CFD run directory. "
            "Select from pareto_front_points.csv, pareto_engineering_ranked.csv or the training CSV.")
    missing = [name for name in (*variables, *REQUIRED_PERFORMANCE) if name not in fields]
    if missing:
        raise ConversionError(
            f"{path} is missing {source} columns {missing}; check --source, or pass --design-variables "
            "when the run used a custom variable list.")


def values_equal(text, wanted):
    try:
        return float(text) == float(wanted)
    except ValueError:
        return text == wanted


def select_row(rows, select=None, row_number=None):
    """Return (index, line_number, row, selection_text) for exactly one data row."""
    if row_number is not None:
        if not 1 <= row_number <= len(rows):
            raise ConversionError(f"--row {row_number} is outside 1..{len(rows)}")
        index = row_number - 1
        return index, rows[index][0], rows[index][1], f"row={row_number}"
    if select:
        key, separator, wanted = select.partition("=")
        key, wanted = key.strip(), wanted.strip()
        if not separator or not key:
            raise ConversionError(f"--select expects COLUMN=VALUE, got {select!r}")
        if key not in rows[0][1]:
            raise ConversionError(f"--select column {key!r} is not in the results CSV")
        hits = [index for index, (_, row) in enumerate(rows) if values_equal(row.get(key, ""), wanted)]
        if len(hits) != 1:
            raise ConversionError(f"--select {key}={wanted} matches {len(hits)} rows; it must match exactly one")
        index = hits[0]
        return index, rows[index][0], rows[index][1], f"{key}={wanted}"
    if len(rows) == 1:
        return 0, rows[0][0], rows[0][1], "row=1 (only row)"
    raise ConversionError(f"The results CSV has {len(rows)} rows; choose one with --select COLUMN=VALUE or --row N")


def load_variable_names(path, source):
    if path is None:
        return SOURCE_VARIABLES[source]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        names = tuple(str(item["name"]) for item in payload["variables"])
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ConversionError(f"--design-variables {path}: expected {{'variables': [{{'name': ...}}]}} ({exc})") from None
    if not names or len(set(names)) != len(names):
        raise ConversionError(f"--design-variables {path}: variable names must be unique and non-empty")
    return names


# --------------------------------------------------------------------------- run directory

def same_vector(expected, candidate, variables):
    """True when every design variable of candidate equals the selected row."""
    for name in variables:
        if name not in candidate:
            return False
        a, b = expected[name], candidate[name]
        if name in INTEGER_VARIABLES:
            if int(round(a)) != int(round(b)):
                return False
        elif abs(a - b) > VECTOR_ATOL + VECTOR_RTOL * abs(a):
            return False
    return True


def vector_differences(expected, candidate, variables):
    return [f"{name}: row {expected[name]:g} vs run {candidate[name]:g}" if name in candidate
            else f"{name}: missing in run" for name in variables
            if not same_vector({name: expected[name]}, candidate, (name,))]


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ConversionError(f"{path}: {exc}") from None


def numeric_dict(payload, variables):
    values = {}
    for name in variables:
        try:
            number = float(payload[name])
        except (KeyError, TypeError, ValueError):
            continue
        if math.isfinite(number):
            values[name] = number
    return values


def read_audit_vectors(path, variables):
    """run_id -> design vector of succeeded runs in an impeller failure_records.csv."""
    if path is None:
        return {}
    fields, rows = read_results(path)
    if "run_id" not in fields:
        raise ConversionError(f"--audit-csv {path} has no run_id column")
    vectors = {}
    for _, row in rows:
        if row.get("status", "").lower() in ("succeeded", "success") and row.get("run_id"):
            vectors[row["run_id"]] = numeric_dict(row, variables)
    return vectors


def recorded_vector(run_dir, source, variables, audit):
    """(vector, origin) recorded in a run directory, or (None, None)."""
    if source == "impeller":
        path = run_dir / "input_parameters.json"
        if path.is_file():
            return numeric_dict(read_json(path), variables), path.name
        if run_dir.name in audit:
            return audit[run_dir.name], f"audit CSV run_id={run_dir.name}"
    else:
        path = run_dir / "candidate.json"
        if path.is_file():
            payload = read_json(path)
            return numeric_dict(payload.get("variables", {}), variables), path.name
    return None, None


def blade_run_dir_candidates(row, runs_roots, results_path):
    run_id = row.get("run_id", "")
    recorded = row.get("case_dir", "")
    if recorded:
        yield Path(recorded)
    if run_id:
        for root in runs_roots:
            yield root / run_id
            yield root / "cases" / run_id
        yield results_path.parent / "cases" / run_id


def locate_run_dir(args, row, vector, variables, notes):
    """Return (run_dir, how_located, vector_check)."""
    audit = read_audit_vectors(args.audit_csv, variables)
    if args.run_dir is not None:
        run_dir = args.run_dir.resolve()
        if not run_dir.is_dir():
            raise ConversionError(f"--run-dir does not exist: {run_dir}")
        located = "--run-dir"
    elif args.source == "blade":
        run_dir = next((path.resolve() for path in blade_run_dir_candidates(row, args.runs_root, args.results)
                        if path.is_dir()), None)
        if run_dir is None:
            raise ConversionError(
                f"Blade case {row.get('run_id') or '?'} not found at the recorded case_dir "
                f"{row.get('case_dir') or '(empty)'}; pass --runs-root <blade output dir> or --run-dir.")
        recorded = Path(row["case_dir"]).resolve() if row.get("case_dir") else None
        located = "recorded case_dir" if run_dir == recorded else f"run_id {row.get('run_id')} under {run_dir.parent}"
    else:
        if not args.runs_root:
            raise ConversionError("Impeller rows carry no run ID; pass --runs-root (Runs/, ActiveLearning_Runs/) or --run-dir.")
        matches, scanned, unreadable = [], 0, []
        for root in args.runs_root:
            if not root.is_dir():
                raise ConversionError(f"--runs-root does not exist: {root}")
            for child in sorted(path for path in root.iterdir() if path.is_dir()):
                try:
                    candidate, _ = recorded_vector(child, args.source, variables, audit)
                except ConversionError:
                    # One interrupted run must not hide the rest of the workspace.
                    unreadable.append(child.name)
                    continue
                if candidate is None:
                    continue
                scanned += 1
                if same_vector(vector, candidate, variables):
                    matches.append(child.resolve())
        if unreadable:
            notes.append(f"Skipped {len(unreadable)} run directories with unreadable input_parameters.json: "
                         f"{', '.join(unreadable[:5])}{' ...' if len(unreadable) > 5 else ''}")
        solved = [path for path in matches if (path / "CFX_Results.txt").is_file()]
        if len(matches) > 1 and len(solved) == 1:
            # A design re-run after a failed attempt shares its vector; only the solved run has results.
            notes.append(f"{len(matches) - 1} other directories hold the same design without CFX_Results.txt.")
            matches = solved
        if len(matches) != 1:
            listing = "".join(f"\n  {path}" for path in matches[:10])
            raise ConversionError(
                f"{len(matches)} of {scanned} recorded run directories match the selected design vector"
                f"{listing}\nPass --run-dir to choose one explicitly.")
        run_dir = matches[0]
        located = f"design-vector search over {scanned} run directories"

    candidate, origin = recorded_vector(run_dir, args.source, variables, audit)
    if candidate is None:
        notes.append(f"{run_dir} records no design vector; it was not checked against the selected row.")
        return run_dir, located, {"status": "unavailable"}
    differences = vector_differences(vector, candidate, variables)
    if differences:
        raise ConversionError(f"{run_dir} ({origin}) does not hold the selected design:\n  " + "\n  ".join(differences))
    return run_dir, located, {"status": "match", "recorded_in": origin}


def file_record(path):
    stat = path.stat()
    modified = datetime.datetime.fromtimestamp(stat.st_mtime, datetime.timezone.utc)
    return {"path": str(path.resolve()), "size_bytes": stat.st_size,
            "modified_utc": modified.isoformat(timespec="seconds")}


def latest_result(run_dir, notes):
    """Same rule as the optimizers: the newest .out must have its own .res."""
    outs = sorted(run_dir.glob("*.out"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    if outs and outs[-1].with_suffix(".res").is_file():
        return outs[-1].with_suffix(".res")
    results = sorted(run_dir.glob("*.res"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    if results:
        notes.append(f"{results[-1].name} has no matching newest .out; using the newest .res by time.")
        return results[-1]
    return None


def locate_artifacts(run_dir, notes):
    """Return (solver_input, solver_input_kind, initial_result, cfx_state)."""
    state_path = run_dir / "cfx_state.json"
    state = read_json(state_path) if state_path.is_file() else None
    result = None
    if state is not None:
        solve = state.get("stages", {}).get("solve", {})
        if solve.get("status") in ("complete", "review") and solve.get("result_file"):
            result = run_dir / solve["result_file"]
            if not result.is_file():
                raise ConversionError(f"cfx_state.json names {solve['result_file']}, which is missing from {run_dir}")
            if solve["status"] == "review":
                notes.append("The blade case's final solve was accepted under manual review (near-converged).")
        else:
            # The receipt is authoritative: any other .res is a failed or unaccepted attempt.
            raise ConversionError(f"{state_path} records solve status {solve.get('status')!r} without an accepted "
                                  "result; this case has no result to start a sweep from.")
    if result is None:
        result = latest_result(run_dir, notes)

    definition = run_dir / "Impeller.def"
    if not definition.is_file():
        definitions = sorted(run_dir.glob("*.def"))
        definition = definitions[0] if len(definitions) == 1 else None
    if definition is None and result is None:
        raise ConversionError(f"{run_dir} contains neither a .def nor a .res; there is nothing to start a sweep from.")
    if definition is None:
        notes.append("No .def in the run directory (the impeller runner deletes Impeller.def after post-processing); "
                      "cfx.ps1 will use the .res as solver input. Verify on one speed line first.")
        return result, "res", result, state
    if result is None:
        notes.append("No .res in the run directory; cfx.ps1 will start every speed line from a zero field.")
    return definition, "def", result, state


def read_cfx_results(run_dir, passages):
    """Full-impeller values from CFX_Results.txt (one passage's flow and power)."""
    path = run_dir / "CFX_Results.txt"
    if not path.is_file():
        return None
    try:
        values = [float(item) for item in path.read_text(encoding="utf-8-sig").strip().split(",")]
    except (OSError, ValueError):
        return None
    if len(values) != len(CFX_RESULT_FIELDS) or not all(math.isfinite(value) for value in values):
        return None
    result = dict(zip(CFX_RESULT_FIELDS, values))
    result["Power"] *= passages
    result["MassFlow"] *= passages
    return result


def performance_check(recorded, cfx):
    if cfx is None:
        return {"status": "unavailable"}
    mismatches = {}
    for name, value in recorded.items():
        if value is None or name not in cfx:
            continue
        if abs(cfx[name] - value) > PERFORMANCE_RTOL * max(abs(value), 1e-12):
            mismatches[name] = {"results_csv": value, "CFX_Results.txt": cfx[name]}
    return {"status": "mismatch" if mismatches else "match", "mismatches": mismatches}


# --------------------------------------------------------------------------- operating point

def resolve_operating_point(args, row, state, notes):
    inputs = (state or {}).get("inputs", {})
    recorded_p_out = optional_finite(row, "P_out")
    p_out_source = "row P_out"
    if recorded_p_out is None and "p_out_pa" in inputs:
        recorded_p_out, p_out_source = finite(inputs["p_out_pa"], "cfx_state.json p_out_pa"), "cfx_state.json p_out_pa"
    if args.design_p_out_pa is not None:
        if recorded_p_out is not None and abs(recorded_p_out - args.design_p_out_pa) > 1e-9:
            notes.append(f"--design-p-out-pa {args.design_p_out_pa:g} overrides the recorded {recorded_p_out:g} Pa.")
        p_out, p_out_source = finite(args.design_p_out_pa, "--design-p-out-pa"), "--design-p-out-pa"
    elif recorded_p_out is None:
        raise ConversionError("The design back pressure is not recorded; pass --design-p-out-pa.")
    else:
        p_out = recorded_p_out

    recorded_passages, passage_source = None, None
    if args.source == "impeller":
        recorded_passages, passage_source = int(round(finite(row["nBl"], "nBl"))), "row nBl"
    elif "n_blades" in inputs:
        recorded_passages, passage_source = int(inputs["n_blades"]), "cfx_state.json n_blades"
    if args.passage_count is not None:
        if recorded_passages is not None and recorded_passages != args.passage_count:
            notes.append(f"--passage-count {args.passage_count} overrides the recorded {recorded_passages}.")
        passages, passage_source = args.passage_count, "--passage-count"
    elif recorded_passages is None:
        raise ConversionError("The passage count is not recorded; pass --passage-count.")
    else:
        passages = recorded_passages
    if passages < 1:
        raise ConversionError(f"Passage count must be positive, got {passages}")

    if args.design_rpm is None:
        rpm, rpm_source = DEFAULT_DESIGN_RPM, "default (optimizer design speed)"
    else:
        rpm, rpm_source = finite(args.design_rpm, "--design-rpm"), "--design-rpm"
    if rpm <= 0:
        raise ConversionError("--design-rpm must be positive")
    return {"speed_rpm": rpm, "speed_source": rpm_source,
            "back_pressure_pa": p_out, "back_pressure_source": p_out_source,
            "passage_count": passages, "passage_source": passage_source}


def speed_lines(args, design_rpm, notes):
    if args.speeds_rpm:
        try:
            speeds = sorted({int(round(float(item))) for item in args.speeds_rpm.split(",") if item.strip()})
        except ValueError:
            raise ConversionError(f"--speeds-rpm must be comma-separated numbers, got {args.speeds_rpm!r}") from None
        if int(round(design_rpm)) not in speeds:
            notes.append(f"--speeds-rpm does not include the design speed {design_rpm:g} rpm.")
    else:
        if not (args.speed_step > 0 and args.speed_span >= 0):
            raise ConversionError("--speed-step must be positive and --speed-span non-negative")
        count = round(args.speed_span / args.speed_step)
        if abs(count * args.speed_step - args.speed_span) > 1e-9:
            raise ConversionError("--speed-span must be a whole multiple of --speed-step")
        speeds = sorted({int(round(design_rpm * (1 + k * args.speed_step))) for k in range(-count, count + 1)})
    if not speeds or speeds[0] <= 0:
        raise ConversionError(f"Speed lines must be positive rpm, got {speeds}")
    return speeds


def check_pressures(initial, design, notes):
    if not math.isfinite(initial):
        raise ConversionError("--initial-pressure-pa must be finite")
    if initial >= MAX_SAFE_PRESSURE_PA:
        raise ConversionError(
            f"--initial-pressure-pa {initial:g} is at or above cfx.ps1's safety limit {MAX_SAFE_PRESSURE_PA:g} Pa; "
            "no speed line would be solved.")
    if design < initial:
        notes.append(f"Design back pressure {design:g} Pa is below the initial {initial:g} Pa; "
                     "the sweep starts past the design point.")
    if design > MAX_SAFE_PRESSURE_PA:
        notes.append(f"Design back pressure {design:g} Pa exceeds cfx.ps1's safety limit {MAX_SAFE_PRESSURE_PA:g} Pa; "
                     "the design speed line will stop before reaching it.")


# --------------------------------------------------------------------------- CCL

BACK_PRESSURE_BOUNDARY_TYPES = {"OUTLET", "OPENING"}
_CCL_BLOCK = re.compile(r"^[A-Z][A-Z0-9 _]*:")
_CCL_ASSIGNMENT = re.compile(r"^\s*([^=:#]+?)\s*=\s*(.*?)\s*$")


def parse_ccl(text):
    """(expressions, assignments) where assignments are (block path, name, value) outside EXPRESSIONS."""
    text = re.sub(r"\\[ \t]*\r?\n[ \t]*", " ", text)  # join continuation lines
    expressions, assignments, stack = {}, [], []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.upper() == "END":
            if stack:
                stack.pop()
        elif _CCL_BLOCK.match(stripped) and "=" not in stripped:
            stack.append(stripped)
        elif match := _CCL_ASSIGNMENT.match(stripped):
            if stack and stack[-1].upper() == "EXPRESSIONS:":
                expressions[match[1]] = match[2]
            else:
                assignments.append((tuple(stack), match[1], match[2]))
    return expressions, assignments


def references(value, name, expressions, depth=0):
    """True if value uses expression `name`, directly or through other expressions."""
    if re.search(rf"\b{re.escape(name)}\b", value):
        return True
    if depth >= 8:
        return False
    return any(references(expressions[token], name, expressions, depth + 1)
               for token in expressions if token != name and re.search(rf"\b{re.escape(token)}\b", value))


def expression_binding(ccl_text, bind_domain=None):
    """Do MySpeed / MyBackPressure reach the domain speed and a boundary of the solver input?"""
    expressions, assignments = parse_ccl(ccl_text)

    def domain(path):
        return next((block.split(":", 1)[1].strip() for block in reversed(path)
                     if block.upper().startswith("DOMAIN:")), "")

    speeds = [(domain(path), value) for path, name, value in assignments if name == "Angular Velocity"]
    unbound = {name for name, value in speeds if not references(value, "MySpeed", expressions)}
    via_override = bind_domain is not None and bind_domain in unbound
    unbound.discard(bind_domain)
    speed_bound = bool(speeds) and not unbound

    def boundary(path):
        return next((path[:index + 1] for index in range(len(path) - 1, -1, -1)
                     if path[index].upper().startswith("BOUNDARY:")), None)

    # Only an outlet/opening boundary sets the back pressure; a monitor, initial
    # value or other boundary using MyBackPressure leaves the outlet hard-coded.
    types = {path: value.strip().upper() for path, name, value in assignments
             if name == "Boundary Type" and boundary(path) == path}
    uses = [(boundary(path) is not None and types.get(boundary(path)) in BACK_PRESSURE_BOUNDARY_TYPES,
             " / ".join((*path, f"{name} = {value}")))
            for path, name, value in assignments if references(value, "MyBackPressure", expressions)]
    pressure_uses = [text for at_outlet, text in uses if at_outlet]
    return {
        "status": "bound" if speed_bound and pressure_uses else "not_bound",
        "speed": {"bound": speed_bound, "angular_velocity": [value for _, value in speeds],
                  "rotating_domains": sorted({name for name, _ in speeds}), "via_bind_speed_domain": via_override},
        "back_pressure": {"bound": bool(pressure_uses), "used_by": pressure_uses,
                          "ignored_uses": [text for at_outlet, text in uses if not at_outlet]},
    }


def base_ccl_text(rpm, p_out, bind_domain=None, flow_analysis="Flow Analysis 1"):
    # cfx.ps1 rewrites each "MySpeed = ..." / "MyBackPressure = ..." line per point,
    # so each expression stays on one line and no other line may contain them.
    lines = ["LIBRARY:", "  CEL:", "    EXPRESSIONS:",
             f"      MyBackPressure = {p_out:g} [Pa]",
             f"      MySpeed = {rpm:g} [rev min^-1]",
             "    END", "  END", "END"]
    if bind_domain:
        lines += [f"FLOW: {flow_analysis}", f"  DOMAIN: {bind_domain}", "    DOMAIN MODELS:",
                  "      DOMAIN MOTION:", "        Angular Velocity = MySpeed", "      END",
                  "    END", "  END", "END"]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- provenance

def sanitize_url(url):
    """Drop credentials that a remote URL may carry."""
    parts = urlsplit(url)
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def git_info(path):
    """{"repository", "commit", "dirty"} of the git work tree holding path, or None."""
    directory = path if path.is_dir() else path.parent

    def git(*command):
        result = subprocess.run(["git", "-C", str(directory), *command], capture_output=True, text=True, timeout=20)
        return result.stdout.strip() if result.returncode == 0 else None

    try:
        commit = git("rev-parse", "HEAD")
        if not commit:
            return None
        remote = git("config", "--get", "remote.origin.url")
        dirty = bool(git("status", "--porcelain", "--untracked-files=no"))
        top = git("rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return None
    return {"repository": sanitize_url(remote) if remote else top, "commit": commit, "dirty": dirty}


def source_provenance(args, notes):
    info = None
    if args.source_repo and Path(args.source_repo).is_dir():
        info = git_info(Path(args.source_repo))
        if info is None:
            notes.append(f"--source-repo {args.source_repo} is not a git checkout.")
    elif args.source_repo:
        info = {"repository": sanitize_url(args.source_repo), "commit": None, "dirty": None}
    else:
        info = git_info(args.results.resolve())
    info = dict(info or {"repository": None, "commit": None, "dirty": None})
    if args.source_commit:
        if info.get("commit") and info["commit"] != args.source_commit:
            notes.append(f"--source-commit {args.source_commit} differs from the checkout's {info['commit']}.")
        info["commit"], info["dirty"] = args.source_commit, None
    if not info.get("commit"):
        notes.append("The optimizer commit is unknown; pass --source-repo <checkout> or --source-commit.")
    return info


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def display_command(argv):
    return " ".join(f'"{item}"' if not item or re.search(r'[\s"]', item) else item for item in argv)


# --------------------------------------------------------------------------- outputs

def design_point_row(performance, operating, result_name):
    passages = operating["passage_count"]
    row = dict.fromkeys(MAP_COLUMNS, "")
    row.update({
        "schema_version": "1",
        "Result_File": result_name,
        "Speed_RPM": f"{operating['speed_rpm']:g}",
        "Back_Pressure_Pa": f"{operating['back_pressure_pa']:g}",
        # Optimizer MassFlow is already the full impeller in g/s; Blade_Count
        # records that it represents `passages` passages.
        "Mass_Flow_kg_s": repr(performance["MassFlow"] / 1000.0),
        "Static_PR": "" if performance["PressureRatio"] is None else repr(performance["PressureRatio"]),
        "Total_PR": repr(performance["totalpressureratio"]),
        "Isentropic_Efficiency": repr(performance["Efficiency"]),
        "Blade_Count": str(passages),
        "Mass_Flow_Unit": "g/s",
    })
    return row


def write_csv(path, fieldnames, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def design_label(args, selection, run_dir):
    picked = selection if run_dir.name in selection else f"{selection}, {run_dir.name}"
    label = f"{args.source} {picked}"
    return f"SYNTHETIC · {label}" if args.data_source.lower().startswith("synthetic") else label


def convert(args):
    """Write the sweep inputs and manifest; return the manifest dict."""
    notes = []
    variables = load_variable_names(args.design_variables, args.source)
    fields, rows = read_results(args.results)
    check_columns(fields, args.source, variables, args.results)
    index, line_number, row, selection = select_row(rows, args.select, args.row)
    if args.source == "blade" and row.get("status", "success").lower() not in ("", "success"):
        raise ConversionError(f"The selected blade row has status {row['status']!r}; only successful cases have results.")
    vector = {name: finite(row[name], f"{args.results}:{line_number} {name}") for name in variables}
    performance = {name: optional_finite(row, name) for name in PERFORMANCE_COLUMNS}
    for name in REQUIRED_PERFORMANCE:
        if performance[name] is None or performance[name] <= 0:
            raise ConversionError(f"{args.results}:{line_number}: {name} must be a positive number")

    output = args.output_dir.resolve()
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ConversionError(f"--output-dir must be new or empty: {output}")

    run_dir, located, vector_check = locate_run_dir(args, row, vector, variables, notes)
    solver_input, solver_kind, initial_result, state = locate_artifacts(run_dir, notes)
    operating = resolve_operating_point(args, row, state, notes)
    cfx = read_cfx_results(run_dir, operating["passage_count"])
    perf_check = performance_check(performance, cfx)
    if perf_check["status"] == "mismatch":
        notes.append(f"CFX_Results.txt differs from the results CSV: {sorted(perf_check['mismatches'])}; "
                     "the design point keeps the CSV record.")
    static_source = "results CSV"
    if performance["PressureRatio"] is None and cfx is not None:
        performance["PressureRatio"], static_source = cfx["PressureRatio"], "CFX_Results.txt"
    elif performance["PressureRatio"] is None:
        static_source = None
        notes.append("Static pressure ratio is unknown; the design point can only be plotted with --pr-type total.")
    if args.pr_type == "static" and performance["PressureRatio"] is None:
        raise ConversionError("--pr-type static needs a static pressure ratio, which neither the CSV nor "
                              "CFX_Results.txt records; use --pr-type total.")

    speeds = speed_lines(args, operating["speed_rpm"], notes)
    check_pressures(args.initial_pressure_pa, operating["back_pressure_pa"], notes)

    binding = {"status": "unchecked"}
    if args.def_ccl is not None:
        try:
            ccl_text = args.def_ccl.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ConversionError(f"--def-ccl: {exc}") from None
        binding = expression_binding(ccl_text, args.bind_speed_domain)
        if binding["status"] != "bound":
            problems = []
            if not binding["speed"]["bound"]:
                problems.append("rotor speed ignores MySpeed (Angular Velocity = "
                                f"{', '.join(binding['speed']['angular_velocity']) or 'none'}; rotating domains "
                                f"{binding['speed']['rotating_domains'] or 'none'}) - rerun with --bind-speed-domain <domain>")
            if not binding["back_pressure"]["bound"]:
                ignored = binding["back_pressure"]["ignored_uses"]
                problems.append("no OUTLET/OPENING boundary uses MyBackPressure - the template outlet must reference it"
                                + (f" (only used by: {'; '.join(ignored)})" if ignored else ""))
            raise ConversionError(f"{args.def_ccl}: cfx.ps1 could not vary the operating point: " + "; ".join(problems))
    else:
        notes.append("MySpeed/MyBackPressure binding unchecked; run cfx5cmds -read -def <solver input> -text def.ccl "
                     "and pass --def-ccl def.ccl before sweeping.")

    output.mkdir(parents=True, exist_ok=True)
    scan_path, ccl_path = output / "scan_points.csv", output / "base_map.ccl"
    design_path, manifest_path = output / "design_point.csv", output / "manifest.json"
    map_csv, map_png = output / "Compressor_Map_Data.csv", output / f"compressor_map_{args.pr_type}.png"
    write_csv(scan_path, ("SpeedRPM", "InitialPressurePa"),
              [{"SpeedRPM": speed, "InitialPressurePa": f"{args.initial_pressure_pa:g}"} for speed in speeds])
    ccl_path.write_text(base_ccl_text(operating["speed_rpm"], operating["back_pressure_pa"],
                                      args.bind_speed_domain, args.flow_analysis), encoding="utf-8")
    result_name = initial_result.name if initial_result is not None else solver_input.name
    write_csv(design_path, MAP_COLUMNS, [design_point_row(performance, operating, result_name)])

    label = design_label(args, selection, run_dir)
    cfx_argv = ["pwsh", "-NoProfile", "-File", str(ROOT / "cfx.ps1"),
                "-DefFile", str(solver_input), "-BaseCclFile", str(ccl_path)]
    if initial_result is not None:
        cfx_argv += ["-InitialResFile", str(initial_result)]
    cfx_argv += ["-SpeedPressureTablePath", str(scan_path), "-CsvFile", str(map_csv),
                 "-BladeCount", str(operating["passage_count"]), "-MassFlowUnit", args.mass_flow_unit,
                 "-WorkingDirectory", str(output)]
    plot_argv = ["python", str(ROOT / "plot_compressor_map.py"), "--input", str(map_csv),
                 "--efficiency-input", "", "--style", args.plot_style, "--pr-type", args.pr_type,
                 "--blade-count", str(operating["passage_count"]), "--design-point", str(design_path),
                 "--design-label", label, "--output", str(map_png)]

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "generator": {"tool": "workingmap/from_optimization.py", **(git_info(ROOT) or {})},
        "source": {
            "optimizer": args.source,
            "data_source": args.data_source,
            **source_provenance(args, notes),
            "results_csv": str(args.results.resolve()),
            "results_sha256": sha256(args.results),
            "selection": selection,
            "row_number": index + 1,
            "line_number": line_number,
            "row_identity": {key: row[key] for key in ("engineering_rank", "front_index", "run_id") if row.get(key)},
        },
        "design_vector": vector,
        "recorded_performance": {
            "Efficiency": performance["Efficiency"],
            "totalpressureratio": performance["totalpressureratio"],
            "PressureRatio": performance["PressureRatio"],
            "PressureRatio_source": static_source,
            "MassFlow_g_s": performance["MassFlow"],
            "Power": performance["Power"],
            "definitions": {"totalpressureratio": "stationary-frame mass-flow-averaged total-to-total",
                            "PressureRatio": "mass-flow-averaged static-to-static",
                            "MassFlow_g_s": "full impeller (all passages)"},
        },
        "operating_point": operating,
        "run": {
            "directory": str(run_dir),
            "located_by": located,
            "solver_input": {"kind": solver_kind, **file_record(solver_input)},
            "initial_result": file_record(initial_result) if initial_result is not None else None,
        },
        "checks": {"design_vector": vector_check, "performance": perf_check, "expression_binding": binding},
        "sweep": {"design_speed_rpm": operating["speed_rpm"], "speeds_rpm": speeds,
                  "initial_pressure_pa": args.initial_pressure_pa, "max_safe_pressure_pa": MAX_SAFE_PRESSURE_PA,
                  "mass_flow_unit": args.mass_flow_unit, "pr_type": args.pr_type, "plot_style": args.plot_style},
        "outputs": {"scan_points": scan_path.name, "base_ccl": ccl_path.name, "design_point": design_path.name,
                    "map_csv": map_csv.name, "map_png": map_png.name},
        "commands": {"cfx_sweep": {"argv": cfx_argv, "display": display_command(cfx_argv)},
                     "plot": {"argv": plot_argv, "display": display_command(plot_argv)}},
        "notes": notes,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                             encoding="utf-8")
    return manifest


def main(argv=None):
    args = parse_args(argv)
    try:
        manifest = convert(args)
    except ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    output = Path(args.output_dir).resolve()
    for note in manifest["notes"]:
        print(f"note: {note}", file=sys.stderr)
    print(f"Run directory: {manifest['run']['directory']} ({manifest['run']['located_by']})")
    print(f"Wrote {', '.join(manifest['outputs'][key] for key in ('scan_points', 'base_ccl', 'design_point'))} "
          f"and manifest.json to {output}")
    print(f"Sweep:  {manifest['commands']['cfx_sweep']['display']}")
    print(f"Plot:   {manifest['commands']['plot']['display']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
