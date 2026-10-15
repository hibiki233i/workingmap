import contextlib
import csv
import hashlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

import from_optimization as fo
import plot_compressor_map as plot

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'examples' / 'from_optimization'

IMPELLER_VECTOR = dict(d1s=0.39295, dH=0.04919, beta1hb=77.856, beta1sb=31.582, d2=0.52785, b2=0.05599,
                       beta2hb=48.32, beta2sb=40.514, Lz=0.1874, t=0.002342, TipClear=0.000939, nBl=11.0,
                       rake_te_s=-15.018)
BLADE_VECTOR = {name: 0.25 * index for index, name in enumerate(fo.SOURCE_VARIABLES['blade'])}

BOUND_CCL = '''LIBRARY:
  CEL:
    EXPRESSIONS:
      MyBackPressure = 12 [Pa]
      MySpeed = 10000 [rev min^-1]
      RotorSpeed = MySpeed
    END
  END
END
FLOW: Flow Analysis 1
  DOMAIN: R1
    DOMAIN MODELS:
      DOMAIN MOTION:
        Angular Velocity = RotorSpeed
        Option = Rotating
      END
    END
    BOUNDARY: R1 Outlet
      Boundary Type = OUTLET
      BOUNDARY CONDITIONS:
        MASS AND MOMENTUM:
          Option = Average Static Pressure
          Relative Pressure = \\
            MyBackPressure
        END
      END
    END
  END
END
'''


def load_example_module(name):
    spec = importlib.util.spec_from_file_location(f'example_{name}', EXAMPLE / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_csv(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def read_csv(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


class ConverterTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()  # macOS: /tmp is a symlink
        self.out = self.root / 'out'

    def convert(self, *argv):
        return fo.convert(fo.parse_args([*argv, '--output-dir', str(self.out)]))

    # -- impeller fixtures: rows carry no run ID; run directories hold input_parameters.json
    def impeller_row(self, rank, **changes):
        row = {'engineering_rank': str(rank), 'front_index': str(30 + rank)}
        row.update({name: repr(value) for name, value in IMPELLER_VECTOR.items()})
        row.update(P_out='12.0', Efficiency='0.7486677355674427', totalpressureratio='1.6819375',
                   Power='109.66145833333331', MassFlow='4.49925')
        row.update(changes)
        return row

    def impeller_run(self, name, vector=None, results=True, definition=False):
        run_dir = self.root / 'Runs' / name
        run_dir.mkdir(parents=True)
        vector = dict(IMPELLER_VECTOR if vector is None else vector)
        (run_dir / 'input_parameters.json').write_text(json.dumps({**vector, 'P_out': 12.0}), encoding='utf-8')
        if results:
            (run_dir / 'CFX_Results.txt').write_text(
                f'0.7486677355674427,1.1855,{109.66145833333331 / 11!r},{4.49925 / 11!r},1.6819375', encoding='utf-8')
        (run_dir / 'Impeller_001.out').write_text('out', encoding='utf-8')
        (run_dir / 'Impeller_001.res').write_text('res', encoding='utf-8')
        if definition:
            (run_dir / 'Impeller.def').write_text('def', encoding='utf-8')
        return run_dir

    def impeller_results(self, rows=None):
        return write_csv(self.root / 'pareto_engineering_ranked.csv',
                         rows or [self.impeller_row(1), self.impeller_row(2, d1s='0.38')])

    # -- blade fixtures: rows carry run_id and the case_dir recorded on the optimization machine
    def blade_case(self, run_id='case_000004', result_file='Impeller_001.res', status='complete', output='blade_output'):
        case = self.root / output / 'cases' / run_id
        case.mkdir(parents=True)
        (case / 'candidate.json').write_text(json.dumps({'run_id': run_id, 'variables': BLADE_VECTOR}),
                                             encoding='utf-8')
        (case / 'cfx_state.json').write_text(json.dumps({
            'version': 2, 'inputs': {'run_id': run_id, 'p_out_pa': 11.5, 'n_blades': 9},
            'stages': {'solve': {'status': status, 'exit_code': 0, 'result_file': result_file}}}), encoding='utf-8')
        for name in ('Impeller.def', 'Impeller_001.out', 'Impeller_001.res'):
            (case / name).write_text(name, encoding='utf-8')
        return case

    def blade_results(self, **changes):
        row = {name: repr(value) for name, value in BLADE_VECTOR.items()}
        row.update(Efficiency='0.7628', PressureRatio='2.0', MassFlow='4.25', Power='1.0',
                   totalpressureratio='2.1', status='success', run_id='case_000004',
                   case_dir=r'D:\blade_runs\cases\case_000004')
        row.update(changes)
        return write_csv(self.root / 'pareto_front.csv', [row])


class ImpellerTests(ConverterTestCase):
    def test_vector_search_writes_sweep_inputs_and_provenance(self):
        results = self.impeller_results()
        self.impeller_run('Run_000', dict(IMPELLER_VECTOR, d1s=0.38))
        target = self.impeller_run('Run_031')
        manifest = self.convert('--source', 'impeller', '--results', str(results), '--select', 'engineering_rank=1',
                                '--runs-root', str(self.root / 'Runs'), '--source-repo', 'https://example.org/impeller.git',
                                '--source-commit', 'abc123', '--data-source', 'synthetic_analytic_v1')

        self.assertEqual(manifest['run']['directory'], str(target.resolve()))
        self.assertIn('over 2 run directories', manifest['run']['located_by'])
        self.assertEqual(manifest['checks']['design_vector']['status'], 'match')
        self.assertEqual(manifest['checks']['performance']['status'], 'match')
        # The impeller runner deletes Impeller.def; the solved .res becomes the solver input.
        self.assertEqual(manifest['run']['solver_input']['kind'], 'res')
        self.assertTrue(manifest['run']['solver_input']['path'].endswith('Impeller_001.res'))
        source = manifest['source']
        self.assertEqual((source['repository'], source['commit'], source['line_number'], source['row_number']),
                         ('https://example.org/impeller.git', 'abc123', 2, 1))
        self.assertEqual(source['results_sha256'], hashlib.sha256(results.read_bytes()).hexdigest())
        self.assertEqual(manifest['design_vector'], IMPELLER_VECTOR)
        self.assertEqual(manifest['recorded_performance']['PressureRatio_source'], 'CFX_Results.txt')
        self.assertEqual(manifest['operating_point']['passage_count'], 11)
        self.assertEqual(manifest['sweep']['speeds_rpm'], list(range(8000, 12001, 500)))

        scan = read_csv(self.out / 'scan_points.csv')
        self.assertEqual([row['SpeedRPM'] for row in scan], [str(s) for s in range(8000, 12001, 500)])
        self.assertEqual({row['InitialPressurePa'] for row in scan}, {'5'})
        self.assertIn('SYNTHETIC', manifest['commands']['plot']['argv'][manifest['commands']['plot']['argv'].index('--design-label') + 1])

    def test_design_point_plots_the_recorded_performance(self):
        self.impeller_run('Run_031')
        manifest = self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                                '--select', 'front_index=31', '--runs-root', str(self.root / 'Runs'))
        design = self.out / 'design_point.csv'
        total = plot.load_rows(design, blade_count=11, pr_type='total')[0]
        static = plot.load_rows(design, blade_count=11, pr_type='static')[0]
        self.assertAlmostEqual(total['mass_flow_gs'], 4.49925, places=12)
        self.assertEqual((total['speed'], total['target_pressure']), (10000, 12))
        self.assertAlmostEqual(total['pressure_ratio'], 1.6819375, places=12)
        self.assertAlmostEqual(static['pressure_ratio'], 1.1855, places=12)
        self.assertAlmostEqual(total['efficiency_pct'], 74.86677355674427, places=10)
        self.assertEqual(read_csv(design)[0]['Blade_Count'], '11')
        argv = manifest['commands']['plot']['argv']
        self.assertEqual((argv[argv.index('--style') + 1], argv[argv.index('--pr-type') + 1], argv[-1]),
                         ('paper', 'static', str(self.out / 'compressor_map_static.png')))

    def test_existing_def_is_preferred_and_res_is_the_initial_field(self):
        self.impeller_run('Run_031', definition=True)
        manifest = self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                                '--select', 'engineering_rank=1', '--runs-root', str(self.root / 'Runs'))
        self.assertEqual(manifest['run']['solver_input']['kind'], 'def')
        argv = manifest['commands']['cfx_sweep']['argv']
        self.assertTrue(argv[argv.index('-DefFile') + 1].endswith('Impeller.def'))
        self.assertTrue(argv[argv.index('-InitialResFile') + 1].endswith('Impeller_001.res'))
        self.assertEqual((argv[argv.index('-BladeCount') + 1], argv[argv.index('-MassFlowUnit') + 1]), ('11', 'g/s'))

    def test_search_needs_exactly_one_match(self):
        results = self.impeller_results()
        self.impeller_run('Run_000', dict(IMPELLER_VECTOR, d1s=0.38))
        with self.assertRaisesRegex(fo.ConversionError, '0 of 1'):
            self.convert('--source', 'impeller', '--results', str(results), '--select', 'engineering_rank=1',
                         '--runs-root', str(self.root / 'Runs'))
        self.impeller_run('Run_031')
        self.impeller_run('AL_Iter01_P1_A00001')
        with self.assertRaisesRegex(fo.ConversionError, '(?s)2 of 3.*--run-dir'):
            self.convert('--source', 'impeller', '--results', str(results), '--select', 'engineering_rank=1',
                         '--runs-root', str(self.root / 'Runs'))
        self.assertFalse(self.out.exists())

    def test_failed_attempts_and_unreadable_runs_do_not_block_the_search(self):
        self.impeller_run('AL_Iter01_P1_A00001', results=False)  # failed attempt of the same design
        target = self.impeller_run('AL_Iter01_P1_A00002')
        broken = self.root / 'Runs' / 'AL_Iter01_P2_A00003'
        broken.mkdir()
        (broken / 'input_parameters.json').write_text('{"d1s": 0.3', encoding='utf-8')  # interrupted write
        manifest = self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                                '--select', 'engineering_rank=1', '--runs-root', str(self.root / 'Runs'))
        self.assertEqual(manifest['run']['directory'], str(target))
        notes = ' '.join(manifest['notes'])
        self.assertIn('without CFX_Results.txt', notes)
        self.assertIn('AL_Iter01_P2_A00003', notes)

    def test_rounded_blade_count_still_matches(self):
        # Historical DOE inputs keep the unsnapped LHS value; the runners execute round(nBl).
        self.impeller_run('Run_031', dict(IMPELLER_VECTOR, nBl=10.6))
        manifest = self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                                '--select', 'engineering_rank=1', '--runs-root', str(self.root / 'Runs'))
        self.assertEqual(manifest['checks']['design_vector']['status'], 'match')

    def test_audit_csv_supplies_vectors_for_runs_without_input_parameters(self):
        run_dir = self.impeller_run('AL_Iter01_P1_A00007')
        (run_dir / 'input_parameters.json').unlink()
        audit = write_csv(self.root / 'failure_records.csv', [
            {'run_id': 'AL_Iter01_P1_A00007', 'source': 'active_learning', 'status': 'succeeded',
             **{name: repr(value) for name, value in IMPELLER_VECTOR.items()}}])
        manifest = self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                                '--select', 'engineering_rank=1', '--runs-root', str(self.root / 'Runs'),
                                '--audit-csv', str(audit))
        self.assertEqual(manifest['checks']['design_vector']['recorded_in'], 'audit CSV run_id=AL_Iter01_P1_A00007')

    def test_explicit_run_dir_must_hold_the_selected_design(self):
        run_dir = self.impeller_run('Run_007', dict(IMPELLER_VECTOR, beta2hb=50.0))
        with self.assertRaisesRegex(fo.ConversionError, 'beta2hb: row 48.32 vs run 50'):
            self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                         '--select', 'engineering_rank=1', '--run-dir', str(run_dir))

    def test_performance_mismatch_is_reported_but_the_csv_record_is_kept(self):
        self.impeller_run('Run_031')
        results = self.impeller_results([self.impeller_row(1, Efficiency='0.70')])
        manifest = self.convert('--source', 'impeller', '--results', str(results), '--runs-root', str(self.root / 'Runs'))
        self.assertEqual(manifest['checks']['performance']['status'], 'mismatch')
        self.assertIn('Efficiency', manifest['checks']['performance']['mismatches'])
        self.assertEqual(read_csv(self.out / 'design_point.csv')[0]['Isentropic_Efficiency'], '0.7')

    def test_static_ratio_unknown_allows_total_only(self):
        self.impeller_run('Run_031', results=False)
        args = ['--source', 'impeller', '--results', str(self.impeller_results()), '--select', 'engineering_rank=1',
                '--runs-root', str(self.root / 'Runs')]
        with self.assertRaisesRegex(fo.ConversionError, 'static pressure ratio.*use --pr-type total'):
            self.convert(*args)
        manifest = self.convert(*args, '--pr-type', 'total')
        self.assertEqual(read_csv(self.out / 'design_point.csv')[0]['Static_PR'], '')
        self.assertTrue(any('Static pressure ratio is unknown' in note for note in manifest['notes']))

    def test_surrogate_predictions_are_rejected(self):
        surrogate = write_csv(self.root / 'nsga2_surrogate_pareto.csv', [
            {**{name: repr(value) for name, value in IMPELLER_VECTOR.items()}, 'pred_Efficiency': '0.75',
             'pred_totalpressureratio': '1.7', 'pred_MassFlow': '4.4'}])
        with self.assertRaisesRegex(fo.ConversionError, 'surrogate'):
            self.convert('--source', 'impeller', '--results', str(surrogate), '--runs-root', str(self.root))

    def test_selection_must_be_unique_and_explicit(self):
        results = self.impeller_results()
        for argv, message in ((['--select', 'P_out=12'], 'matches 2 rows'), ([], 'choose one'),
                              (['--row', '3'], 'outside 1..2'), (['--select', 'missing=1'], 'not in the results')):
            with self.subTest(argv=argv), self.assertRaisesRegex(fo.ConversionError, message):
                self.convert('--source', 'impeller', '--results', str(results), '--runs-root', str(self.root), *argv)

    def test_output_directory_must_be_new_or_empty(self):
        self.impeller_run('Run_031')
        self.out.mkdir()
        (self.out / 'manifest.json').write_text('{}', encoding='utf-8')
        with self.assertRaisesRegex(fo.ConversionError, 'new or empty'):
            self.convert('--source', 'impeller', '--results', str(self.impeller_results()),
                         '--select', 'engineering_rank=1', '--runs-root', str(self.root / 'Runs'))
        self.assertEqual((self.out / 'manifest.json').read_text(encoding='utf-8'), '{}')


class BladeTests(ConverterTestCase):
    def test_case_recorded_on_another_machine_is_found_under_runs_root(self):
        case = self.blade_case()
        # A stray newer .res must not replace the result recorded in cfx_state.json.
        (case / 'Impeller_002.out').write_text('newer', encoding='utf-8')
        (case / 'Impeller_002.res').write_text('newer', encoding='utf-8')
        manifest = self.convert('--source', 'blade', '--results', str(self.blade_results()),
                                '--select', 'run_id=case_000004', '--runs-root', str(self.root / 'blade_output'))
        self.assertEqual(manifest['run']['directory'], str(case.resolve()))
        self.assertTrue(manifest['run']['located_by'].startswith('run_id case_000004'))
        self.assertEqual(manifest['run']['solver_input']['kind'], 'def')
        self.assertTrue(manifest['run']['initial_result']['path'].endswith('Impeller_001.res'))
        self.assertEqual(manifest['checks']['design_vector'], {'status': 'match', 'recorded_in': 'candidate.json'})
        operating = manifest['operating_point']
        self.assertEqual((operating['back_pressure_pa'], operating['back_pressure_source']),
                         (11.5, 'cfx_state.json p_out_pa'))
        self.assertEqual((operating['passage_count'], operating['passage_source']), (9, 'cfx_state.json n_blades'))
        self.assertEqual(read_csv(self.out / 'design_point.csv')[0]['Blade_Count'], '9')

    def test_default_cases_directory_next_to_results(self):
        case = self.blade_case(output='')
        manifest = self.convert('--source', 'blade', '--results', str(self.blade_results()))
        self.assertEqual(manifest['run']['directory'], str(case.resolve()))

    def test_missing_case_names_the_fix(self):
        with self.assertRaisesRegex(fo.ConversionError, 'case_000004 not found.*--runs-root'):
            self.convert('--source', 'blade', '--results', str(self.blade_results()))

    def test_failed_rows_and_unsolved_cases(self):
        with self.assertRaisesRegex(fo.ConversionError, "status 'failed'"):
            self.convert('--source', 'blade', '--results', str(self.blade_results(status='failed')))
        case = self.blade_case(result_file='missing.res')
        with self.assertRaisesRegex(fo.ConversionError, 'missing.res'):
            self.convert('--source', 'blade', '--results', str(self.blade_results()), '--run-dir', str(case))

    def test_unaccepted_solve_is_rejected_despite_a_res_file(self):
        # Codex review P2: never fall back to a .res the receipt did not accept.
        for status in ('failed', 'running', 'needs_restart'):
            with self.subTest(status=status):
                case = self.blade_case(run_id=f'case_{status}', status=status)
                with self.assertRaisesRegex(fo.ConversionError, f"solve status '{status}' without an accepted"):
                    self.convert('--source', 'blade', '--results', str(self.blade_results()), '--run-dir', str(case))
                self.assertFalse(self.out.exists())


class SweepTests(ConverterTestCase):
    def setUp(self):
        super().setUp()
        self.blade_case()
        self.base = ['--source', 'blade', '--results', str(self.blade_results()),
                     '--runs-root', str(self.root / 'blade_output')]

    def test_speed_lines_center_on_design_speed(self):
        manifest = self.convert(*self.base, '--design-rpm', '9000', '--speed-span', '0.1', '--speed-step', '0.05',
                                '--initial-pressure-pa', '6.5')
        self.assertEqual(manifest['sweep']['speeds_rpm'], [8100, 8550, 9000, 9450, 9900])
        self.assertEqual({row['InitialPressurePa'] for row in read_csv(self.out / 'scan_points.csv')}, {'6.5'})
        self.assertEqual(read_csv(self.out / 'design_point.csv')[0]['Speed_RPM'], '9000')

    def test_explicit_speeds_and_invalid_sweeps(self):
        manifest = self.convert(*self.base, '--speeds-rpm', '9000, 11000,9000')
        self.assertEqual(manifest['sweep']['speeds_rpm'], [9000, 11000])
        self.assertTrue(any('does not include the design speed' in note for note in manifest['notes']))
        for argv, message in ((['--speed-span', '0.12'], 'whole multiple'),
                              (['--initial-pressure-pa', '16'], 'safety limit'),
                              (['--speed-step', '0'], 'positive')):
            with self.subTest(argv=argv), self.assertRaisesRegex(fo.ConversionError, message):
                fo.convert(fo.parse_args([*self.base, *argv, '--output-dir', str(self.root / 'other')]))

    def test_design_pressure_outside_the_sweep_is_noted(self):
        manifest = self.convert(*self.base, '--design-p-out-pa', '17')
        notes = ' '.join(manifest['notes'])
        self.assertIn('overrides the recorded 11.5', notes)
        self.assertIn('exceeds cfx.ps1', notes)

    def test_base_ccl_survives_cfx_ps1_substitution(self):
        self.convert(*self.base)
        text = (self.out / 'base_map.ccl').read_text(encoding='utf-8')
        # Same line-wise replacements as New-TempCcl in cfx.ps1.
        lines = [re.sub(r'MyBackPressure\s*=\s*.*', 'MyBackPressure = 7.5 [Pa]',
                        re.sub(r'MySpeed\s*=\s*.*', 'MySpeed = 8500 [rev min^-1]', line)) for line in text.splitlines()]
        expressions, _ = fo.parse_ccl('\n'.join(lines))
        self.assertEqual(expressions, {'MyBackPressure': '7.5 [Pa]', 'MySpeed': '8500 [rev min^-1]'})
        self.assertEqual(fo.parse_ccl(text)[0], {'MyBackPressure': '11.5 [Pa]', 'MySpeed': '10000 [rev min^-1]'})


class ExpressionBindingTests(ConverterTestCase):
    def test_bound_through_expression_chain_and_continuation_lines(self):
        report = fo.expression_binding(BOUND_CCL)
        self.assertEqual(report['status'], 'bound')
        self.assertEqual(report['speed']['rotating_domains'], ['R1'])
        self.assertIn('Relative Pressure = MyBackPressure', report['back_pressure']['used_by'][0])

    def test_hard_coded_speed_needs_the_domain_override(self):
        fixed = BOUND_CCL.replace('Angular Velocity = RotorSpeed', 'Angular Velocity = 10000 [rev min^-1]')
        self.assertEqual(fo.expression_binding(fixed)['status'], 'not_bound')
        self.assertEqual(fo.expression_binding(fixed, 'R2')['status'], 'not_bound')
        report = fo.expression_binding(fixed, 'R1')
        self.assertEqual((report['status'], report['speed']['via_bind_speed_domain']), ('bound', True))

        self.blade_case()
        def_ccl = self.root / 'def.ccl'
        def_ccl.write_text(fixed, encoding='utf-8')
        base = ['--source', 'blade', '--results', str(self.blade_results()), '--runs-root',
                str(self.root / 'blade_output'), '--def-ccl', str(def_ccl)]
        with self.assertRaisesRegex(fo.ConversionError, 'ignores MySpeed.*--bind-speed-domain'):
            self.convert(*base)
        manifest = self.convert(*base, '--bind-speed-domain', 'R1')
        self.assertEqual(manifest['checks']['expression_binding']['status'], 'bound')
        override = fo.parse_ccl((self.out / 'base_map.ccl').read_text(encoding='utf-8'))[1]
        self.assertIn((('FLOW: Flow Analysis 1', 'DOMAIN: R1', 'DOMAIN MODELS:', 'DOMAIN MOTION:'),
                       'Angular Velocity', 'MySpeed'), override)

    def test_unused_back_pressure_is_reported(self):
        report = fo.expression_binding(BOUND_CCL.replace('MyBackPressure\n', '12 [Pa]\n'))
        self.assertEqual((report['status'], report['back_pressure']['bound']), ('not_bound', False))

    def test_back_pressure_must_reach_an_outlet_boundary(self):
        # Codex review P1: a monitor or another boundary using the expression leaves the outlet hard-coded.
        outlet_fixed = BOUND_CCL.replace('MyBackPressure\n', '12 [Pa]\n')
        hard_coded = outlet_fixed[:outlet_fixed.rindex('  END\nEND\n')] + ('''    BOUNDARY: R1 Inlet
      Boundary Type = INLET
      BOUNDARY CONDITIONS:
        MASS AND MOMENTUM:
          Relative Pressure = MyBackPressure
        END
      END
    END
  END
  OUTPUT CONTROL:
    MONITOR OBJECTS:
      MONITOR POINT: Back pressure
        Expression Value = MyBackPressure
      END
    END
  END
END
''')
        report = fo.expression_binding(hard_coded)
        self.assertEqual((report['status'], report['back_pressure']['used_by']), ('not_bound', []))
        self.assertEqual(len(report['back_pressure']['ignored_uses']), 2)
        self.blade_case()
        def_ccl = self.root / 'def.ccl'
        def_ccl.write_text(hard_coded, encoding='utf-8')
        with self.assertRaisesRegex(fo.ConversionError, 'no OUTLET/OPENING boundary.*only used by'):
            self.convert('--source', 'blade', '--results', str(self.blade_results()),
                         '--runs-root', str(self.root / 'blade_output'), '--def-ccl', str(def_ccl))


class ProvenanceTests(unittest.TestCase):
    def test_credentials_are_removed_from_remote_urls(self):
        self.assertEqual(fo.sanitize_url('https://user:token@github.com:443/a/b.git'), 'https://github.com:443/a/b.git')
        self.assertEqual(fo.sanitize_url('git@github.com:a/b.git'), 'git@github.com:a/b.git')

    def test_generator_commit_is_recorded(self):
        info = fo.git_info(ROOT)
        if info is None:
            self.skipTest('workingmap is not a git checkout here')
        self.assertRegex(info['commit'], r'^[0-9a-f]{40}$')

    def test_design_caption_shows_the_recorded_performance(self):
        point = dict(mass_flow_gs=4.49925, pressure_ratio=1.6819375, efficiency_pct=74.87, speed=10000.0,
                     target_pressure=12.0)
        caption = plot.design_point_caption(point, 'total', 'impeller engineering_rank=1, Run_031')
        self.assertEqual(caption.splitlines(), [
            '☆ Design point · impeller engineering_rank=1, Run_031',
            'η 74.9 %  ·  Total PR 1.682  ·  4.50 g/s',
            '10000 rpm, back pressure 12 Pa'])


class OfflineExampleTests(unittest.TestCase):
    def test_one_command_reproduces_both_chains(self):
        example = load_example_module('run_example')
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / 'chain'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(example.main(['--output-dir', str(output)]), 0)
            expected = {'impeller': ('engineering_rank=1', 'Run_031', 4.49925, 1.6819375, 1.244625, 11),
                        'blade': ('run_id=case_000004', 'case_000004', 4.250168528720441, 2.1, 2.0, 9)}
            for name, (selection, run_id, flow, total, static, passages) in expected.items():
                with self.subTest(source=name):
                    manifest = json.loads((output / name / 'manifest.json').read_text(encoding='utf-8'))
                    self.assertEqual(manifest['source']['selection'], selection)
                    self.assertEqual(Path(manifest['run']['directory']).name, run_id)
                    self.assertEqual({key: value['status'] for key, value in manifest['checks'].items()},
                                     {'design_vector': 'match', 'performance': 'match', 'expression_binding': 'bound'})
                    self.assertEqual((output / name / 'compressor_map_static.png').read_bytes()[:8],
                                     b'\x89PNG\r\n\x1a\n')
                    design = plot.load_rows(output / name / 'design_point.csv', passages, 'total')[0]
                    self.assertAlmostEqual(design['mass_flow_gs'], flow, places=9)
                    self.assertAlmostEqual(design['pressure_ratio'], total, places=12)
                    static_point = plot.load_rows(output / name / 'design_point.csv', passages, 'static')[0]
                    self.assertAlmostEqual(static_point['pressure_ratio'], static, places=12)
                    # The synthetic sweep's design speed line passes through the recorded point.
                    on_line = [row for row in plot.load_rows(output / name / 'Compressor_Map_Data.csv', passages, 'total')
                               if (row['speed'], row['target_pressure']) == (10000, 12)]
                    self.assertEqual(len(on_line), 1)
                    self.assertAlmostEqual(on_line[0]['mass_flow_gs'], flow, places=9)
                    self.assertAlmostEqual(on_line[0]['pressure_ratio'], total, places=9)


if __name__ == '__main__':
    unittest.main()
