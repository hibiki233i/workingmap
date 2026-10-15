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


if __name__ == '__main__':
    unittest.main()
