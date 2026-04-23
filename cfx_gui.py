import csv
import json
import locale
import os
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText


ROOT_DIR = Path(__file__).resolve().parent
APP_VERSION = "Version 1.0"
LOG_ENCODING = locale.getpreferredencoding(False) or "utf-8"
SETTINGS_PATH = ROOT_DIR / ".cfx_gui_settings.json"
FLOW_UNITS = ("kg/s", "g/s")


class ScanRowEditor(ttk.Frame):
    def __init__(self, master: tk.Misc, on_change):
        super().__init__(master)
        self.on_change = on_change
        self.rows: list[dict[str, tk.StringVar | ttk.Entry | ttk.Button | ttk.Label]] = []

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for index, title in enumerate(("序号", "转速 RPM", "初始压力 Pa", "操作")):
            ttk.Label(header, text=title).grid(row=0, column=index, padx=4, sticky="w")
        header.columnconfigure(1, weight=1)
        header.columnconfigure(2, weight=1)

        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        body = ttk.Frame(self)
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(body, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")

        scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)

        self.rows_frame = ttk.Frame(self.canvas)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.rows_frame, anchor="nw")

        self.rows_frame.bind("<Configure>", self._sync_scroll_region)
        self.canvas.bind("<Configure>", self._resize_canvas_window)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        for widgets in list(self.rows):
            widgets["index_label"].destroy()
            widgets["speed_entry"].destroy()
            widgets["pressure_entry"].destroy()
            widgets["delete_button"].destroy()
        self.rows.clear()
        for speed, pressure in rows:
            self.add_row(speed=speed, pressure=pressure, trigger_change=False)
        self._refresh_indices()
        self.on_change()

    def add_row(self, speed: str = "", pressure: str = "", trigger_change: bool = True) -> None:
        row_index = len(self.rows)
        speed_var = tk.StringVar(value=speed)
        pressure_var = tk.StringVar(value=pressure)

        index_label = ttk.Label(self.rows_frame, text=str(row_index + 1))
        speed_entry = ttk.Entry(self.rows_frame, textvariable=speed_var, width=16)
        pressure_entry = ttk.Entry(self.rows_frame, textvariable=pressure_var, width=16)
        delete_button = ttk.Button(self.rows_frame, text="删除", command=lambda: self.delete_row(row_index))

        index_label.grid(row=row_index, column=0, padx=4, pady=2, sticky="w")
        speed_entry.grid(row=row_index, column=1, padx=4, pady=2, sticky="ew")
        pressure_entry.grid(row=row_index, column=2, padx=4, pady=2, sticky="ew")
        delete_button.grid(row=row_index, column=3, padx=4, pady=2, sticky="e")

        self.rows_frame.columnconfigure(1, weight=1)
        self.rows_frame.columnconfigure(2, weight=1)

        speed_var.trace_add("write", lambda *_: self.on_change())
        pressure_var.trace_add("write", lambda *_: self.on_change())

        self.rows.append(
            {
                "index_label": index_label,
                "speed_var": speed_var,
                "pressure_var": pressure_var,
                "speed_entry": speed_entry,
                "pressure_entry": pressure_entry,
                "delete_button": delete_button,
            }
        )
        self._refresh_indices()
        if trigger_change:
            self.on_change()
        self.canvas.after_idle(lambda: self.canvas.yview_moveto(1.0))

    def delete_row(self, index: int) -> None:
        if not 0 <= index < len(self.rows):
            return
        widgets = self.rows.pop(index)
        widgets["index_label"].destroy()
        widgets["speed_entry"].destroy()
        widgets["pressure_entry"].destroy()
        widgets["delete_button"].destroy()
        self._regrid_rows()
        self._refresh_indices()
        self.on_change()

    def _regrid_rows(self) -> None:
        for index, widgets in enumerate(self.rows):
            widgets["index_label"].grid_configure(row=index)
            widgets["speed_entry"].grid_configure(row=index)
            widgets["pressure_entry"].grid_configure(row=index)
            widgets["delete_button"].configure(command=lambda idx=index: self.delete_row(idx))
            widgets["delete_button"].grid_configure(row=index)

    def _refresh_indices(self) -> None:
        for index, widgets in enumerate(self.rows):
            widgets["index_label"].configure(text=str(index + 1))

    def _sync_scroll_region(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _resize_canvas_window(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=event.width)

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        if widget is None or not self._is_descendant(widget):
            return
        delta = event.delta
        if delta == 0:
            return
        self.canvas.yview_scroll(int(-delta / 120), "units")

    def _is_descendant(self, widget: tk.Misc) -> bool:
        current = widget
        while current is not None:
            if current == self:
                return True
            current = current.master
        return False

    def get_rows(self) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for widgets in self.rows:
            result.append(
                {
                    "SpeedRPM": str(widgets["speed_var"].get()).strip(),
                    "InitialPressurePa": str(widgets["pressure_var"].get()).strip(),
                }
            )
        return result


class CfxGui(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=12)
        self.master.title(f"CFX 扫描启动器 {APP_VERSION}")
        self.master.geometry("980x760")
        self.grid(sticky="nsew")
        master.columnconfigure(0, weight=1)
        master.rowconfigure(0, weight=1)

        self.process: subprocess.Popen[str] | None = None
        self.temp_csv_path: Path | None = None
        self.stop_requested = False
        self.close_after_stop = False

        self.working_dir_var = tk.StringVar(value=str(ROOT_DIR))
        self.def_file_var = tk.StringVar(value="")
        self.base_ccl_var = tk.StringVar(value="")
        self.initial_res_var = tk.StringVar(value="")
        self.output_csv_var = tk.StringVar(value=str(ROOT_DIR / "Compressor_Map_Data.csv"))
        self.scan_csv_var = tk.StringVar(value=str(ROOT_DIR / "scan_points.csv"))
        self.cores_var = tk.StringVar(value="8")
        self.blade_count_var = tk.StringVar(value="1")
        self.flow_unit_var = tk.StringVar(value="kg/s")
        self.row_count_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="就绪")

        self._build_form()
        self._load_settings()
        self._bind_setting_traces()
        self._load_scan_csv(Path(self.scan_csv_var.get()))
        self.master.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_form(self) -> None:
        path_frame = ttk.LabelFrame(self, text="路径与运行参数", padding=10)
        path_frame.grid(row=0, column=0, sticky="ew")
        path_frame.columnconfigure(1, weight=1)

        self._add_path_row(path_frame, 0, "工作目录", self.working_dir_var, self._browse_directory)
        self._add_path_row(path_frame, 1, "DEF 文件", self.def_file_var, lambda: self._browse_file(self.def_file_var, [("DEF files", "*.def"), ("All files", "*.*")]))
        self._add_path_row(path_frame, 2, "Base CCL", self.base_ccl_var, lambda: self._browse_file(self.base_ccl_var, [("CCL files", "*.ccl"), ("All files", "*.*")]))
        self._add_path_row(path_frame, 3, "初场 RES", self.initial_res_var, lambda: self._browse_file(self.initial_res_var, [("RES files", "*.res"), ("All files", "*.*")]))
        self._add_path_row(path_frame, 4, "输出 CSV", self.output_csv_var, self._browse_output_csv)
        self._add_path_row(path_frame, 5, "扫描 CSV", self.scan_csv_var, self._browse_scan_csv)

        ttk.Label(path_frame, text="CPU 核数").grid(row=6, column=0, sticky="w", pady=4)
        ttk.Entry(path_frame, textvariable=self.cores_var, width=12).grid(row=6, column=1, sticky="w", pady=4)
        ttk.Label(path_frame, text="叶片数").grid(row=7, column=0, sticky="w", pady=4)
        ttk.Entry(path_frame, textvariable=self.blade_count_var, width=12).grid(row=7, column=1, sticky="w", pady=4)
        ttk.Label(path_frame, text="流量单位").grid(row=8, column=0, sticky="w", pady=4)
        ttk.Combobox(path_frame, textvariable=self.flow_unit_var, values=FLOW_UNITS, width=10, state="readonly").grid(row=8, column=1, sticky="w", pady=4)
        version_status = ttk.Frame(path_frame)
        version_status.grid(row=8, column=2, sticky="e", padx=(8, 0))
        ttk.Label(version_status, text=APP_VERSION, foreground="#444").pack(side=tk.LEFT, padx=(0, 12))
        ttk.Label(version_status, textvariable=self.status_var, foreground="#444").pack(side=tk.LEFT)

        scan_frame = ttk.LabelFrame(self, text="扫描点表格", padding=10)
        scan_frame.grid(row=1, column=0, sticky="nsew", pady=(12, 0))
        scan_frame.columnconfigure(0, weight=1)
        scan_frame.rowconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        toolbar = ttk.Frame(scan_frame)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Button(toolbar, text="新增行", command=lambda: self._add_scan_row()).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="从 CSV 载入", command=self._reload_from_scan_csv).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(toolbar, text="导出扫描 CSV", command=self._export_scan_csv).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(toolbar, text="扫描数量:").pack(side=tk.LEFT, padx=(16, 4))
        ttk.Label(toolbar, textvariable=self.row_count_var).pack(side=tk.LEFT)

        self.scan_editor = ScanRowEditor(scan_frame, on_change=self._update_row_count)
        self.scan_editor.grid(row=1, column=0, sticky="nsew")

        action_frame = ttk.Frame(self)
        action_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(action_frame, text="开始扫描", command=self._start_scan).pack(side=tk.LEFT)
        self.stop_button = ttk.Button(action_frame, text="终止扫描", command=self._stop_scan)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(action_frame, text="仅保存扫描 CSV", command=self._export_scan_csv).pack(side=tk.LEFT, padx=(8, 0))
        ttk.Label(action_frame, text=f"界面版本：{APP_VERSION}").pack(side=tk.RIGHT)
        self._set_process_controls(running=False)

        log_frame = ttk.LabelFrame(self, text="运行日志", padding=10)
        log_frame.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        self.rowconfigure(3, weight=1)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_widget = ScrolledText(log_frame, wrap=tk.WORD, height=18, state="disabled")
        self.log_widget.grid(row=0, column=0, sticky="nsew")

    def _add_path_row(self, parent, row: int, label: str, variable: tk.StringVar, browse_command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(8, 8), pady=4)
        ttk.Button(parent, text="浏览", command=browse_command).grid(row=row, column=2, sticky="e", pady=4)

    def _browse_directory(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.working_dir_var.get() or str(ROOT_DIR))
        if selected:
            self.working_dir_var.set(selected)

    def _browse_file(self, variable: tk.StringVar, filetypes) -> None:
        initial = Path(variable.get()).parent if variable.get() else ROOT_DIR
        selected = filedialog.askopenfilename(initialdir=initial, filetypes=filetypes)
        if selected:
            variable.set(selected)

    def _browse_output_csv(self) -> None:
        initial = Path(self.output_csv_var.get()).parent if self.output_csv_var.get() else ROOT_DIR
        selected = filedialog.asksaveasfilename(initialdir=initial, defaultextension=".csv", filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if selected:
            self.output_csv_var.set(selected)

    def _browse_scan_csv(self) -> None:
        initial = Path(self.scan_csv_var.get()).parent if self.scan_csv_var.get() else ROOT_DIR
        selected = filedialog.askopenfilename(initialdir=initial, filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if selected:
            self.scan_csv_var.set(selected)
            self._load_scan_csv(Path(selected))

    def _bind_setting_traces(self) -> None:
        variables = [
            self.working_dir_var,
            self.def_file_var,
            self.base_ccl_var,
            self.initial_res_var,
            self.output_csv_var,
            self.scan_csv_var,
            self.cores_var,
            self.blade_count_var,
            self.flow_unit_var,
        ]
        for variable in variables:
            variable.trace_add("write", lambda *_: self._save_settings())

    def _load_settings(self) -> None:
        if not SETTINGS_PATH.is_file():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return

        mapping = {
            "working_dir": self.working_dir_var,
            "def_file": self.def_file_var,
            "base_ccl": self.base_ccl_var,
            "initial_res": self.initial_res_var,
            "output_csv": self.output_csv_var,
            "scan_csv": self.scan_csv_var,
            "cores": self.cores_var,
            "blade_count": self.blade_count_var,
            "flow_unit": self.flow_unit_var,
        }
        for key, variable in mapping.items():
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                variable.set(value)

        geometry = data.get("window_geometry")
        if isinstance(geometry, str) and geometry.strip():
            self.master.geometry(geometry)

    def _save_settings(self) -> None:
        payload = {
            "working_dir": self.working_dir_var.get().strip(),
            "def_file": self.def_file_var.get().strip(),
            "base_ccl": self.base_ccl_var.get().strip(),
            "initial_res": self.initial_res_var.get().strip(),
            "output_csv": self.output_csv_var.get().strip(),
            "scan_csv": self.scan_csv_var.get().strip(),
            "cores": self.cores_var.get().strip(),
            "blade_count": self.blade_count_var.get().strip(),
            "flow_unit": self.flow_unit_var.get().strip(),
            "window_geometry": self.master.winfo_geometry(),
        }
        try:
            SETTINGS_PATH.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _on_close(self) -> None:
        if self.process is not None and self.process.poll() is None:
            answer = messagebox.askyesno("终止扫描", "扫描仍在运行。关闭窗口前会终止 PowerShell 脚本及其子进程，是否继续？")
            if not answer:
                return
            self._save_settings()
            self.close_after_stop = True
            self._terminate_process_tree()
            return
        self._save_settings()
        self.master.destroy()

    def _set_process_controls(self, running: bool) -> None:
        self.stop_button.configure(state="normal" if running else "disabled")

    def _load_scan_csv(self, path: Path) -> None:
        if not path.exists():
            self.scan_editor.set_rows([("8000", "5"), ("8500", "5"), ("9000", "5")])
            self._log(f"扫描 CSV 不存在，已加载内置默认值: {path}")
            return
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = [(row.get("SpeedRPM", "").strip(), row.get("InitialPressurePa", "").strip()) for row in reader]
        if not rows:
            rows = [("8000", "5")]
        self.scan_editor.set_rows(rows)
        self._log(f"已加载扫描 CSV: {path}")

    def _reload_from_scan_csv(self) -> None:
        self._load_scan_csv(Path(self.scan_csv_var.get()))

    def _add_scan_row(self) -> None:
        self.scan_editor.add_row()

    def _update_row_count(self) -> None:
        self.row_count_var.set(str(len(self.scan_editor.get_rows())))

    def _collect_validated_rows(self) -> list[dict[str, str]]:
        rows = self.scan_editor.get_rows()
        if not rows:
            raise ValueError("扫描表格不能为空。")
        cleaned = []
        for index, row in enumerate(rows, start=1):
            speed = row["SpeedRPM"]
            pressure = row["InitialPressurePa"]
            if not speed or not pressure:
                raise ValueError(f"第 {index} 行缺少转速或初始压力。")
            try:
                int(speed)
            except ValueError as exc:
                raise ValueError(f"第 {index} 行转速不是有效整数: {speed}") from exc
            try:
                float(pressure)
            except ValueError as exc:
                raise ValueError(f"第 {index} 行初始压力不是有效数字: {pressure}") from exc
            cleaned.append({"SpeedRPM": speed, "InitialPressurePa": pressure})
        return cleaned

    def _validate_form(self) -> dict[str, str | list[dict[str, str]]]:
        working_dir = Path(self.working_dir_var.get()).expanduser()
        def_file = Path(self.def_file_var.get()).expanduser()
        base_ccl = Path(self.base_ccl_var.get()).expanduser()
        initial_res_text = self.initial_res_var.get().strip()
        initial_res = Path(initial_res_text).expanduser() if initial_res_text else None
        output_csv = Path(self.output_csv_var.get()).expanduser()
        scan_csv = Path(self.scan_csv_var.get()).expanduser()

        if not working_dir.is_dir():
            raise ValueError(f"工作目录不存在: {working_dir}")
        if not str(def_file).strip() or str(def_file) == ".":
            raise ValueError("请先选择 DEF 文件。")
        if not def_file.is_file():
            raise ValueError(f"DEF 文件不存在: {def_file}")
        if not str(base_ccl).strip() or str(base_ccl) == ".":
            raise ValueError("请先选择 Base CCL 文件。")
        if not base_ccl.is_file():
            raise ValueError(f"Base CCL 不存在: {base_ccl}")
        if initial_res is not None and not initial_res.is_file():
            raise ValueError(f"初场 RES 文件不存在: {initial_res}")
        if not output_csv.parent.exists():
            raise ValueError(f"输出 CSV 目录不存在: {output_csv.parent}")
        if scan_csv and not scan_csv.parent.exists():
            raise ValueError(f"扫描 CSV 目录不存在: {scan_csv.parent}")

        try:
            cores = int(self.cores_var.get().strip())
        except ValueError as exc:
            raise ValueError("CPU 核数必须是整数。") from exc
        if cores <= 0:
            raise ValueError("CPU 核数必须大于 0。")

        try:
            blade_count = float(self.blade_count_var.get().strip())
        except ValueError as exc:
            raise ValueError("叶片数必须是有效数字。") from exc
        if blade_count <= 0:
            raise ValueError("叶片数必须大于 0。")

        flow_unit = self.flow_unit_var.get().strip()
        if flow_unit not in FLOW_UNITS:
            raise ValueError("流量单位只能选择 kg/s 或 g/s。")

        rows = self._collect_validated_rows()
        return {
            "working_dir": str(working_dir),
            "def_file": str(def_file),
            "base_ccl": str(base_ccl),
            "initial_res": str(initial_res) if initial_res is not None else "",
            "output_csv": str(output_csv),
            "scan_csv": str(scan_csv),
            "cores": str(cores),
            "blade_count": str(blade_count),
            "flow_unit": flow_unit,
            "rows": rows,
        }

    def _write_scan_csv(self, target: Path, rows: list[dict[str, str]]) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["SpeedRPM", "InitialPressurePa"])
            writer.writeheader()
            writer.writerows(rows)
        return target

    def _export_scan_csv(self) -> None:
        try:
            payload = self._validate_form()
            path = self._write_scan_csv(Path(str(payload["scan_csv"])), payload["rows"])  # type: ignore[arg-type]
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            self.status_var.set("参数校验失败")
            return
        self.status_var.set("扫描 CSV 已保存")
        self._log(f"已导出扫描 CSV: {path}")

    def _start_scan(self) -> None:
        if self.process is not None and self.process.poll() is None:
            messagebox.showwarning("任务进行中", "已有扫描任务在运行。")
            return

        try:
            payload = self._validate_form()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            self.status_var.set("参数校验失败")
            return

        scan_csv_path = Path(str(payload["scan_csv"]))
        if scan_csv_path.name == "":
            fd, temp_name = tempfile.mkstemp(prefix="scan_points_", suffix=".csv", dir=str(ROOT_DIR))
            os.close(fd)
            self.temp_csv_path = Path(temp_name)
            scan_csv_path = self.temp_csv_path
        self._write_scan_csv(scan_csv_path, payload["rows"])  # type: ignore[arg-type]

        script_path = ROOT_DIR / "cfx.ps1"
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if shell is None:
            shell = "powershell"
            self._log("未在当前环境发现 powershell/pwsh，命令仍会按 Windows 目标环境构造。")

        command = [
            shell,
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
            "-WorkingDirectory",
            str(payload["working_dir"]),
            "-DefFile",
            str(payload["def_file"]),
            "-BaseCclFile",
            str(payload["base_ccl"]),
            "-InitialResFile",
            str(payload["initial_res"]),
            "-CsvFile",
            str(payload["output_csv"]),
            "-SpeedPressureTablePath",
            str(scan_csv_path),
            "-Cores",
            str(payload["cores"]),
            "-BladeCount",
            str(payload["blade_count"]),
            "-MassFlowUnit",
            str(payload["flow_unit"]),
        ]

        self._log("启动命令:")
        self._log(" ".join(command))
        self._log(f"日志解码编码: {LOG_ENCODING}")

        try:
            self.stop_requested = False
            self.close_after_stop = False
            self.process = subprocess.Popen(
                command,
                cwd=str(payload["working_dir"]),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding=LOG_ENCODING,
                errors="replace",
            )
        except OSError as exc:
            self.status_var.set("启动失败")
            messagebox.showerror("启动失败", f"无法启动扫描脚本: {exc}")
            self._log(f"启动失败: {exc}")
            return

        self.status_var.set("扫描已启动")
        self._log("扫描进程已启动。")
        self._set_process_controls(running=True)
        threading.Thread(target=self._stream_process_output, daemon=True).start()

    def _terminate_process_tree(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return

        self.stop_requested = True
        self.status_var.set("正在终止扫描")
        self._log(f"正在终止扫描进程，PID={self.process.pid}")

        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                self.process.terminate()
        except OSError as exc:
            self._log(f"终止进程失败: {exc}")

    def _stop_scan(self) -> None:
        if self.process is None or self.process.poll() is not None:
            messagebox.showinfo("没有运行中的任务", "当前没有正在运行的扫描任务。")
            return
        if not messagebox.askyesno("终止扫描", "确定要终止当前运行的 PowerShell 扫描脚本吗？"):
            return
        self._terminate_process_tree()

    def _stream_process_output(self) -> None:
        assert self.process is not None
        for line in self.process.stdout or []:
            self.master.after(0, self._log, line.rstrip())
        exit_code = self.process.wait()
        self.master.after(0, self._handle_process_exit, exit_code)

    def _handle_process_exit(self, exit_code: int) -> None:
        self.process = None
        self._set_process_controls(running=False)
        if self.stop_requested:
            self.status_var.set("扫描已终止")
            self._log(f"扫描进程已终止，退出码: {exit_code}")
            self.stop_requested = False
            if self.close_after_stop:
                self.close_after_stop = False
                self.master.destroy()
                return
            messagebox.showinfo("已终止", "扫描脚本及其子进程已终止。")
            return

        self.status_var.set(f"扫描结束，退出码 {exit_code}")
        self._log(f"扫描进程结束，退出码: {exit_code}")
        if exit_code == 0:
            messagebox.showinfo("执行完成", "扫描脚本已执行完成。")
        else:
            messagebox.showwarning("执行结束", f"扫描脚本已退出，退出码: {exit_code}")

    def _log(self, message: str) -> None:
        self.log_widget.configure(state="normal")
        self.log_widget.insert(tk.END, message + "\n")
        self.log_widget.see(tk.END)
        self.log_widget.configure(state="disabled")


def main() -> None:
    root = tk.Tk()
    style = ttk.Style(root)
    if "vista" in style.theme_names():
        style.theme_use("vista")
    CfxGui(root)
    root.mainloop()


if __name__ == "__main__":
    main()
