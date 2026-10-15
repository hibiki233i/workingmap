import json
import subprocess
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import cfx_gui as gui


class GuiPressureRatioTests(unittest.TestCase):
    def test_plot_command_passes_pressure_ratio_to_real_plotter(self):
        with tempfile.TemporaryDirectory() as folder:
            for pr_type in ('static', 'total'):
                with self.subTest(pr_type=pr_type):
                    output = Path(folder) / f'{pr_type}.png'
                    command = gui.build_plot_command(dict(
                        input_csv=gui.ROOT_DIR / 'examples/compressor_map.csv',
                        plot_output=output, blade_count=10, fit_method='poly_deg_2',
                        pr_type=pr_type, efficiency_csv=None))
                    self.assertIn('--pr-type', command)
                    self.assertEqual(command[command.index('--pr-type') + 1], pr_type)
                    result = subprocess.run(command, cwd=folder, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_bytes()[:8], b'\x89PNG\r\n\x1a\n')

    def test_selection_settings_and_collected_config(self):
        # Tcl variables exercise settings/config without opening a desktop window.
        interp = tk.Tcl()
        app = gui.CfxGui.__new__(gui.CfxGui)
        app.vars = {key: tk.StringVar(master=interp, value=value) for key, value in dict(
            pr_type='static', fit_method='poly_deg_2', flow_unit='kg/s',
            cores='8', blade_count='10').items()}
        app.pr_display_var = tk.StringVar(master=interp)
        app.fit_display_var = tk.StringVar(master=interp)
        app.auto_plot_var = tk.BooleanVar(master=interp)
        with tempfile.TemporaryDirectory() as folder:
            settings = Path(folder) / 'settings.json'
            settings.write_text(json.dumps({'pr_type': 'total'}))
            with patch.object(gui, 'SETTINGS_PATH', settings):
                app._load_settings()
            self.assertEqual(app.pr_display_var.get(), gui.PR_TYPES['total'])
            app.pr_display_var.set(gui.PR_TYPES['static'])
            app._on_pr_selected()
            self.assertEqual(app.vars['pr_type'].get(), 'static')
            app.pr_display_var.set(gui.PR_TYPES['total'])
            app._on_pr_selected()
            app.resolve = lambda key: {
                'working_dir': Path(folder),
                'output_csv': gui.ROOT_DIR / 'examples/compressor_map.csv',
                'plot_output': Path(folder) / 'map.png',
                'efficiency_csv': None,
            }[key]
            config, errors, _warnings = app._collect_plot_config()
            self.assertEqual(errors, [])
            self.assertEqual(config['pr_type'], 'total')
            app.root = Mock()
            app.root.wm_state.side_effect = tk.TclError('No desktop in this test')
            with patch.object(gui, 'SETTINGS_PATH', settings):
                app._save_settings()
            self.assertEqual(json.loads(settings.read_text())['pr_type'], 'total')
            # Invalid saved choice returns to the compatible default.
            settings.write_text(json.dumps({'pr_type': 'invalid'}))
            with patch.object(gui, 'SETTINGS_PATH', settings):
                app._load_settings()
            self.assertEqual(app.vars['pr_type'].get(), 'static')


if __name__ == '__main__':
    unittest.main()
