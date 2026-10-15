import csv
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

import plot_compressor_map as plot

ROOT = Path(__file__).resolve().parents[1]


class MapDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'points.csv'

    def write_rows(self, rows):
        with self.path.open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    def point(self, **changes):
        row = dict(schema_version='1', Result_File='arbitrary.res', Speed_RPM='50000',
                   Back_Pressure_Pa='12.5', Mass_Flow_kg_s='0.002', Static_PR='1.4',
                   Total_PR='1.6', Blade_Count='10', Mass_Flow_Unit='g/s')
        row.update(changes)
        return row

    def test_explicit_columns_override_filename_and_select_total(self):
        self.write_rows([self.point(Result_File='Map_Speed_1_Press_2.res')])
        row = plot.load_rows(self.path, 10, pr_type='total')[0]
        self.assertEqual((row['speed'], row['target_pressure'], row['pressure_ratio']),
                         (50000, 12.5, 1.6))

    def test_canonical_kg_s_is_not_converted_twice(self):
        self.write_rows([self.point()])
        self.assertAlmostEqual(plot.load_rows(self.path, 20)[0]['mass_flow_gs'], 4)

    def test_legacy_passage_scaling_and_filename_warning(self):
        self.write_rows([dict(Result_File='Map_Speed_40000_Press_-3.5.res',
                             Mass_Flow_kg_s='0.0002', Static_PR='1.2')])
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            row = plot.load_rows(self.path, 10)[0]
        self.assertEqual((row['speed'], row['target_pressure'], row['mass_flow_gs']),
                         (40000, -3.5, 2))
        self.assertTrue(any('filename' in str(w.message) for w in caught))
        self.assertTrue(any('Blade_Count' in str(w.message) for w in caught))

    def test_partial_legacy_metadata_fallback(self):
        self.write_rows([self.point(Speed_RPM='', Result_File='Map_Speed_45000_Press_8.res')])
        with self.assertWarnsRegex(UserWarning, 'filename'):
            row = plot.load_rows(self.path, 10)[0]
        self.assertEqual((row['speed'], row['target_pressure']), (45000, 12.5))

    def test_total_never_substitutes_static(self):
        row = self.point()
        del row['Total_PR']
        self.write_rows([row])
        with self.assertRaisesRegex(ValueError, 'Total_PR'):
            plot.load_rows(self.path, 10, pr_type='total')

    def test_invalid_rows_are_reported(self):
        for changes in [dict(Speed_RPM='oops'), dict(Total_PR=''), dict(Blade_Count='0'),
                        dict(Speed_RPM='', Back_Pressure_Pa='', Result_File='unknown.res')]:
            with self.subTest(changes=changes):
                self.write_rows([self.point(**changes)])
                with self.assertWarns(UserWarning):
                    self.assertEqual(plot.load_rows(self.path, 10, pr_type='total'), [])

    def test_unsupported_version_is_rejected(self):
        self.write_rows([self.point(schema_version='2')])
        with self.assertRaisesRegex(ValueError, 'unsupported schema_version'):
            plot.load_rows(self.path, 10)

    def test_invalid_plot_blade_count(self):
        self.write_rows([self.point()])
        for count in (0, -1, float('nan')):
            with self.assertRaises(ValueError):
                plot.load_rows(self.path, count)

    def test_cli_static_total_and_design_point_png(self):
        self.write_rows([self.point()])
        for pr_type in ('static', 'total'):
            output = Path(self.temp.name) / f'{pr_type}.png'
            result = subprocess.run([sys.executable, str(ROOT / 'plot_compressor_map.py'),
                '--input', str(self.path), '--output', str(output), '--efficiency-input', '',
                '--pr-type', pr_type, '--design-point', str(self.path)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_paper_style_writes_raster_and_vector_figures(self):
        signatures = {'.png': b'\x89PNG\r\n\x1a\n', '.pdf': b'%PDF', '.svg': b'<?xml'}
        for pr_type, suffix in (('static', '.png'), ('total', '.png'), ('static', '.pdf'), ('static', '.svg')):
            with self.subTest(pr_type=pr_type, suffix=suffix):
                output = Path(self.temp.name) / f'paper_{pr_type}{suffix}'
                result = subprocess.run([sys.executable, str(ROOT / 'plot_compressor_map.py'),
                    '--input', str(ROOT / 'examples' / 'compressor_map.csv'), '--output', str(output),
                    '--efficiency-input', '', '--style', 'paper', '--pr-type', pr_type,
                    '--design-point', str(ROOT / 'examples' / 'design_point.csv'), '--design-label', 'example'],
                    capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(output.read_bytes().startswith(signatures[suffix]))

    def test_paper_style_without_efficiency_data(self):
        output = Path(self.temp.name) / 'legacy.png'
        result = subprocess.run([sys.executable, str(ROOT / 'plot_compressor_map.py'),
            '--input', str(ROOT / 'examples' / 'legacy_map.csv'), '--output', str(output),
            '--efficiency-input', '', '--style', 'paper'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_paper_surge_line_spans_only_the_surge_points(self):
        surge = [dict(mass_flow_gs=flow, pressure_ratio=1.0 + flow / 10) for flow in (3.0, 4.0, 5.0)]
        x, y = plot.surge_line_xy({'predictor': lambda flow: 1.0 + flow / 10}, surge)
        self.assertEqual((x[0], x[-1]), (3.0, 5.0))
        self.assertAlmostEqual(y[-1], 1.5)

    def test_paper_note_records_the_design_point(self):
        point = dict(mass_flow_gs=4.49925, pressure_ratio=1.2449, efficiency_pct=74.87, speed=10000.0,
                     target_pressure=12.0)
        self.assertEqual(plot.design_point_note(point, 'static', 'impeller engineering_rank=1').splitlines(), [
            r'Design point (optimization record): $\dot{m}$ = 4.50 g/s, $\pi_\mathrm{s}$ = 1.245, '
            r'$\eta_\mathrm{is}$ = 74.9 %, 10000 rpm, back pressure 12 Pa',
            'Source: impeller engineering_rank=1'])

    def test_legacy_efficiency_csv_does_not_block_total_map(self):
        legacy = Path(self.temp.name) / 'Extracted_Compressor_Data.csv'
        legacy.write_text((ROOT / 'examples' / 'legacy_map.csv').read_text(encoding='utf-8'), encoding='utf-8')
        output = Path(self.temp.name) / 'total.png'
        result = subprocess.run([sys.executable, str(ROOT / 'plot_compressor_map.py'),
            '--input', str(ROOT / 'examples' / 'compressor_map.csv'), '--output', str(output),
            '--efficiency-input', str(legacy), '--pr-type', 'total'],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Ignoring efficiency CSV', result.stderr)
        self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')


if __name__ == '__main__':
    unittest.main()
