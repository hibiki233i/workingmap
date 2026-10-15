"""Offline end-to-end chain: optimization result -> sweep inputs -> map -> PNG.

For the impeller (POLA-1) and blade (POLA-2) synthetic outputs this script
  1. builds stand-in CFD run directories, one per training row, laid out like
     the optimizers' Runs/Run_XXX and cases/case_XXXXXX;
  2. runs from_optimization.py on one Pareto row to locate its run directory and
     write scan_points.csv, base_map.ccl, design_point.csv and manifest.json;
  3. replaces the CFX sweep with an analytic map anchored on the design point;
  4. plots the map with the manifest's plot command (total pressure ratio).

Everything here is SYNTHETIC: placeholder .def/.res files and analytic values.
"""
import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import from_optimization  # noqa: E402
from synthetic_map import write_synthetic_map  # noqa: E402

PLACEHOLDER = "SYNTHETIC placeholder written by examples/from_optimization/run_example.py; not a CFX file.\n"

# Repository state that produced the bundled fixtures (see README.md here).
IMPELLER = {
    "repo": "https://github.com/hibiki233i/cryogenic-compressor-impller-optimization.git",
    "commit": "669908189f8b370eb264b16ae35afc6d6567d469",
    "data_source": "synthetic_analytic_v1",
    "select": "engineering_rank=1",
}
BLADE = {
    "repo": "https://github.com/hibiki233i/compressor_blade_optimizition.git",
    "commit": "ae42ea7e24bef3553e7a50b01dbff032c95c9b32",
    "data_source": "synthetic_offline_baseline",
    "select": "run_id=case_000004",
    # benchmarks/offline_baseline/config.json runtime
    "p_out_pa": 12.0,
    "n_blades": 9,
}

# Two stand-in solver definitions show both outcomes of the --def-ccl check:
# the impeller one hard-codes the rotor speed (fixed by --bind-speed-domain),
# the blade one already drives it with MySpeed. Neither describes a real template.
DEF_CCL = {
    "impeller": "10000 [rev min^-1]",
    "blade": "MySpeed",
}


def read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def cfx_results_line(row, passages):
    # CFX_Results.txt holds one passage's power and flow.
    values = (float(row["Efficiency"]), float(row["PressureRatio"]), float(row["Power"]) / passages,
              float(row["MassFlow"]) / passages, float(row["totalpressureratio"]))
    return ",".join(repr(value) for value in values) + "\n"


def write_placeholders(run_dir, names):
    for name in names:
        (run_dir / name).write_text(PLACEHOLDER, encoding="utf-8")


def build_impeller_runs(training_csv, runs_root):
    """Runs/Run_XXX as the impeller runner leaves them: Impeller.def is deleted after post-processing."""
    names = from_optimization.SOURCE_VARIABLES["impeller"]
    for index, row in enumerate(read_rows(training_csv)):
        run_dir = runs_root / f"Run_{index:03d}"
        run_dir.mkdir(parents=True)
        parameters = {name: float(row[name]) for name in (*names, "P_out")}
        (run_dir / "input_parameters.json").write_text(json.dumps(parameters, indent=2), encoding="utf-8")
        (run_dir / "CFX_Results.txt").write_text(cfx_results_line(row, int(round(parameters["nBl"]))),
                                                 encoding="utf-8")
        write_placeholders(run_dir, ("Impeller_001.out", "Impeller_001.res"))


def build_blade_cases(training_csv, output_root):
    """cases/case_XXXXXX with the blade runner's candidate.json and cfx_state.json receipt."""
    names = from_optimization.SOURCE_VARIABLES["blade"]
    for row in read_rows(training_csv):
        if row["status"] != "success":
            continue
        run_dir = output_root / "cases" / row["run_id"]
        run_dir.mkdir(parents=True)
        candidate = {"run_id": row["run_id"], "variables": {name: float(row[name]) for name in names}}
        state = {"version": 2,
                 "inputs": {"run_id": row["run_id"], "p_out_pa": BLADE["p_out_pa"], "n_blades": BLADE["n_blades"]},
                 "stages": {"pre": {"status": "complete"},
                            "solve": {"status": "complete", "exit_code": 0, "result_file": "Impeller_001.res"},
                            "post": {"status": "complete"}}}
        (run_dir / "candidate.json").write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        (run_dir / "cfx_state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        (run_dir / "CFX_Results.txt").write_text(cfx_results_line(row, BLADE["n_blades"]), encoding="utf-8")
        write_placeholders(run_dir, ("Impeller.def", "Impeller_001.out", "Impeller_001.res"))


def def_ccl_text(angular_velocity):
    return "\n".join([
        "# SYNTHETIC stand-in for: cfx5cmds -read -def <solver input> -text def.ccl",
        "LIBRARY:", "  CEL:", "    EXPRESSIONS:", "      MyBackPressure = 12 [Pa]", "      MySpeed = 10000 [rev min^-1]",
        "    END", "  END", "END",
        "FLOW: Flow Analysis 1", "  DOMAIN: R1", "    DOMAIN MODELS:", "      DOMAIN MOTION:",
        f"        Angular Velocity = {angular_velocity}", "        Option = Rotating", "      END", "    END",
        "    BOUNDARY: R1 Outlet", "      Boundary Type = OUTLET", "      BOUNDARY CONDITIONS:", "        MASS AND MOMENTUM:",
        "          Option = Average Static Pressure", "          Relative Pressure = MyBackPressure",
        "        END", "      END", "    END", "  END", "END", ""])


def run_chain(name, source, results_csv, runs_root, output_root):
    output = output_root / name
    def_ccl = output_root / f"{name}_def.ccl"
    def_ccl.write_text(def_ccl_text(DEF_CCL[name]), encoding="utf-8")
    argv = ["--source", name, "--results", str(results_csv), "--select", source["select"],
            "--runs-root", str(runs_root), "--output-dir", str(output), "--def-ccl", str(def_ccl),
            "--source-repo", source["repo"], "--source-commit", source["commit"],
            "--data-source", source["data_source"]]
    if DEF_CCL[name] != "MySpeed":
        argv += ["--bind-speed-domain", "R1"]
    manifest = from_optimization.convert(from_optimization.parse_args(argv))
    rows = write_synthetic_map(output / manifest["outputs"]["scan_points"],
                               output / manifest["outputs"]["design_point"],
                               output / manifest["outputs"]["map_csv"])
    plot_argv = [sys.executable, *manifest["commands"]["plot"]["argv"][1:]]
    result = subprocess.run(plot_argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{name} plot failed:\n{result.stderr}")
    performance = manifest["recorded_performance"]
    print(f"[{name}] {manifest['source']['selection']} -> {manifest['run']['directory']}")
    print(f"    located by {manifest['run']['located_by']}; solver input {manifest['run']['solver_input']['kind']}; "
          f"checks: vector {manifest['checks']['design_vector']['status']}, "
          f"performance {manifest['checks']['performance']['status']}, "
          f"expressions {manifest['checks']['expression_binding']['status']}")
    print(f"    design point: eta {performance['Efficiency']:.4f}, total PR {performance['totalpressureratio']:.4f}, "
          f"{performance['MassFlow_g_s']:.3f} g/s at {manifest['operating_point']['speed_rpm']:g} rpm, "
          f"{manifest['operating_point']['back_pressure_pa']:g} Pa")
    print(f"    {len(manifest['sweep']['speeds_rpm'])} speed lines, {rows} synthetic map points -> "
          f"{output / manifest['outputs']['map_png']}")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output-dir", type=Path, default=Path("example-output") / "from_optimization",
                        help="New or empty directory (default example-output/from_optimization).")
    parser.add_argument("--impeller-dir", type=Path, default=HERE / "impeller",
                        help="Impeller output with Compressor_Training_Data.csv and pareto_engineering_ranked.csv "
                             "(e.g. a fresh `python -m impeller_app.reproduce` run).")
    parser.add_argument("--blade-dir", type=Path, default=HERE / "blade",
                        help="Blade output with training_data.csv and pareto_front.csv "
                             "(e.g. a fresh `python benchmarks/run_offline_baseline.py` run).")
    args = parser.parse_args(argv)
    output = args.output_dir.resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"output directory must be new or empty: {output}")

    build_impeller_runs(args.impeller_dir / "Compressor_Training_Data.csv", output / "impeller_runs" / "Runs")
    build_blade_cases(args.blade_dir / "training_data.csv", output / "blade_runs")
    try:
        run_chain("impeller", IMPELLER, args.impeller_dir / "pareto_engineering_ranked.csv",
                  output / "impeller_runs" / "Runs", output)
        run_chain("blade", BLADE, args.blade_dir / "pareto_front.csv", output / "blade_runs", output)
    except from_optimization.ConversionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"SYNTHETIC example complete: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
