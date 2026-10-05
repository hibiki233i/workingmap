from __future__ import annotations

import csv
import io
import json
import locale
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from collections import deque
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


ROOT_DIR = Path(__file__).resolve().parent
APP_NAME = "CFX 压气机特性图扫描"
APP_VERSION = "Version 2.0"
SETTINGS_PATH = ROOT_DIR / ".cfx_gui_settings.json"
SCAN_SCRIPT = ROOT_DIR / "cfx.ps1"
PLOT_SCRIPT = ROOT_DIR / "plot_compressor_map.py"
FLOW_UNITS = ("kg/s", "g/s")
FIT_METHODS = {
    "poly_deg_2": "二次多项式",
    "poly_deg_1": "一次多项式",
    "auto": "自动选择（留一法）",
}
DEFAULT_ROWS = [("8000", "5"), ("8500", "5"), ("9000", "5")]
IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CFX_PROCESS_NAMES = ("solver-mpi.exe", "cfx5solve.exe", "cfx5control.exe")
MAX_LOG_LINES = 8000
MAX_BATCH_ROWS = 500

SPEED_RE = re.compile(r"^\+?\d+$")
NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
SPEED_LINE_RE = re.compile(r"开始扫描等转速线[:：]\s*(\d+)")
POINT_RE = re.compile(r"实测流量[:：]")
GEOMETRY_RE = re.compile(r"^(\d+)x(\d+)(?:([+-]-?\d+)([+-]-?\d+))?$")

PALETTE = {
    "bg": "#f1f3f6",
    "card": "#ffffff",
    "border": "#e1e5eb",
    "text": "#1f2937",
    "muted": "#6b7280",
    "faint": "#9ca3af",
    "accent": "#2563eb",
    "accent_hover": "#1d4ed8",
    "accent_press": "#1e40af",
    "accent_soft": "#e8effd",
    "accent_disabled": "#a9c1f5",
    "danger": "#dc2626",
    "danger_soft": "#fdeeee",
    "success": "#15803d",
    "warn": "#b45309",
    "field_border": "#cbd2db",
    "button": "#ffffff",
    "button_hover": "#f3f5f8",
    "button_press": "#e5e9ef",
    "stripe": "#f8fafc",
    "select": "#dbe6fc",
    "header": "#1e293b",
    "header_text": "#f8fafc",
    "header_muted": "#94a3b8",
    "status_bar": "#e6e9ee",
    "log_bg": "#0f172a",
    "log_fg": "#e2e8f0",
}
P = PALETTE

FIELD_STATE_COLORS = {
    "ok": P["success"],
    "warn": "#d97706",
    "error": P["danger"],
    "empty": "#c3c9d2",
}

PILL_STYLES = {
    "idle": ("#334155", "#e2e8f0"),
    "running": ("#2563eb", "#ffffff"),
    "plotting": ("#7c3aed", "#ffffff"),
    "stopping": ("#b45309", "#ffffff"),
    "success": ("#15803d", "#ffffff"),
    "stopped": ("#b45309", "#ffffff"),
    "error": ("#b91c1c", "#ffffff"),
}

LOG_RULES = (
    ("heading", re.compile(r"^=+$|开始扫描等转速线|\[准备提交\]")),
    ("warn", re.compile(r"警告|预警|⚠|Warning|缓存失效|锁墙|堵塞|回退|细分|夹逼|边界|未收敛|起始区异常|骤降", re.I)),
    ("error", re.compile(r"错误|失败|异常|发散|崩溃|Error|Exception|Traceback|\[停止\]|找不到|不存在", re.I)),
    ("success", re.compile(r"成功|完成|收敛|Saved plot|已保存|已生成")),
    ("dim", re.compile(r"^\s*-> (流量下降|三点曲率|\[放宽\])")),
)

UI_SCALE = 1.0


def px(value: float) -> int:
    return int(round(value * UI_SCALE))


# ----------------------------------------------------------------------------
# 纯逻辑工具函数（不依赖界面，便于单独测试）
# ----------------------------------------------------------------------------
def clean_path_text(text: str) -> str:
    text = (text or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def resolve_path(text: str, base: Path) -> Path | None:
    text = clean_path_text(text)
    if not text:
        return None
    path = Path(os.path.expandvars(text)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path


def format_number(value: float) -> str:
    return f"{value:g}"


def validate_scan_row(speed: str, pressure: str) -> str | None:
    speed = speed.strip()
    pressure = pressure.strip()
    if not speed:
        return "缺少转速"
    if not SPEED_RE.match(speed) or not 0 < int(speed) < 2**31:
        return "转速需为正整数"
    if not pressure:
        return "缺少背压"
    if not NUMBER_RE.match(pressure) or not math.isfinite(float(pressure)):
        return "背压需为数字"
    return None


def collect_scan_rows(rows: list[tuple[str, str]]) -> tuple[list[tuple[str, str]], list[str]]:
    cleaned: list[tuple[str, str]] = []
    errors: list[str] = []
    for index, (speed, pressure) in enumerate(rows, start=1):
        speed, pressure = speed.strip(), pressure.strip()
        if not speed and not pressure:
            continue
        error = validate_scan_row(speed, pressure)
        if error:
            errors.append(f"第 {index} 行：{error}")
            continue
        cleaned.append((str(int(speed)), pressure))
    if not cleaned and not errors:
        errors.append("扫描点表格为空。")
    return cleaned, errors


def _fallback_encodings() -> list[str]:
    candidates: list[str] = []
    if IS_WINDOWS:
        try:
            import ctypes

            candidates.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
            candidates.append(f"cp{ctypes.windll.kernel32.GetACP()}")
        except (AttributeError, OSError):
            pass
    candidates.append(locale.getpreferredencoding(False) or "utf-8")
    candidates.append("gb18030")
    result: list[str] = []
    for name in candidates:
        try:
            "".encode(name)
        except LookupError:
            continue
        if name.lower() not in (item.lower() for item in result):
            result.append(name)
    return result


FALLBACK_ENCODINGS = _fallback_encodings()


def decode_output(raw: bytes) -> str:
    # PowerShell 5.1 在重定向时按 OEM 代码页输出，PowerShell 7 / Python 输出 UTF-8，逐行自动识别。
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for encoding in FALLBACK_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def classify_log_line(text: str) -> str | None:
    for tag, pattern in LOG_RULES:
        if pattern.search(text):
            return tag
    return None


def read_scan_csv(path: Path) -> list[tuple[str, str]]:
    text = decode_output(path.read_bytes()).lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(text))
    columns = {(name or "").strip(): name for name in reader.fieldnames or []}
    if "SpeedRPM" not in columns or "InitialPressurePa" not in columns:
        raise ValueError("缺少必需列 SpeedRPM / InitialPressurePa")
    rows: list[tuple[str, str]] = []
    for row in reader:
        speed = (row.get(columns["SpeedRPM"]) or "").strip()
        pressure = (row.get(columns["InitialPressurePa"]) or "").strip()
        if speed or pressure:
            rows.append((speed, pressure))
    return rows


def write_scan_csv(path: Path, rows: list[tuple[str, str]]) -> None:
    temp_path = path.with_name(path.name + ".tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["SpeedRPM", "InitialPressurePa"])
        writer.writerows(rows)
    try:
        os.replace(temp_path, path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


def parse_pasted_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = [part.strip() for part in re.split(r"[\t,;，；]|\s+", line.strip()) if part.strip()]
        if len(parts) < 2:
            continue
        speed, pressure = parts[0], parts[1]
        if NUMBER_RE.match(speed) and NUMBER_RE.match(pressure):
            if re.fullmatch(r"\+?\d+\.0*", speed):
                speed = speed.split(".")[0]
            rows.append((speed, pressure))
    return rows


def generate_speed_rows(start: int, end: int, step: int, pressure: str) -> list[tuple[str, str]]:
    if step <= 0:
        raise ValueError("步长必须大于 0")
    direction = 1 if end >= start else -1
    count = abs(end - start) // step + 1
    if count > MAX_BATCH_ROWS:
        raise ValueError(f"将生成 {count} 行，超过上限 {MAX_BATCH_ROWS}")
    return [(str(start + direction * step * index), pressure) for index in range(count)]


def build_scan_command(shell: str, config: dict, scan_csv: Path) -> list[str]:
    command = [
        shell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCAN_SCRIPT),
        "-WorkingDirectory",
        str(config["working_dir"]),
        "-DefFile",
        str(config["def_file"]),
        "-BaseCclFile",
        str(config["base_ccl"]),
        "-CsvFile",
        str(config["output_csv"]),
        "-SpeedPressureTablePath",
        str(scan_csv),
        "-Cores",
        str(config["cores"]),
        "-BladeCount",
        format_number(config["blade_count"]),
        "-MassFlowUnit",
        config["flow_unit"],
    ]
    # 空字符串参数在 Windows PowerShell 5.1 的 -File 模式下可能被吞掉，未指定初场时直接省略。
    if config.get("initial_res"):
        command.extend(["-InitialResFile", str(config["initial_res"])])
    return command


def build_plot_command(config: dict) -> list[str]:
    command = [
        sys.executable,
        "-u",
        str(PLOT_SCRIPT),
        "--input",
        str(config["input_csv"]),
        "--output",
        str(config["plot_output"]),
        "--blade-count",
        format_number(config["blade_count"]),
        "--fit-method",
        config["fit_method"],
    ]
    if config.get("efficiency_csv"):
        command.extend(["--efficiency-input", str(config["efficiency_csv"])])
    return command


def build_scan_environment(cfx_bin_dir: Path | None) -> dict[str, str]:
    env = os.environ.copy()
    if cfx_bin_dir is not None:
        current = env.get("PATH", "")
        env["PATH"] = str(cfx_bin_dir) + (os.pathsep + current if current else "")
    return env


def find_powershell() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def find_executable(name: str, directory: Path | None = None) -> str | None:
    return shutil.which(name, path=str(directory)) if directory is not None else shutil.which(name)


def read_script_max_pressure() -> float | None:
    try:
        text = SCAN_SCRIPT.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return None
    match = re.search(r"^\s*\$maxSafePressure\s*=\s*([-+]?[\d.]+)", text, re.MULTILINE)
    return float(match.group(1)) if match else None


def ccl_has_scan_expressions(path: Path) -> tuple[bool, bool]:
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return True, True
    return bool(re.search(r"MySpeed\s*=", text)), bool(re.search(r"MyBackPressure\s*=", text))


def find_cfx_processes() -> list[tuple[str, int]]:
    if not IS_WINDOWS:
        return []
    try:
        output = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found: list[tuple[str, int]] = []
    for row in csv.reader(io.StringIO(decode_output(output))):
        if len(row) >= 2 and row[0].lower() in CFX_PROCESS_NAMES:
            try:
                found.append((row[0], int(row[1])))
            except ValueError:
                continue
    return found


def kill_pids(pids: list[int]) -> None:
    if not IS_WINDOWS or not pids:
        return
    command = ["taskkill", "/F", "/T"]
    for pid in pids:
        command.extend(["/PID", str(pid)])
    subprocess.run(command, check=False, capture_output=True, creationflags=CREATE_NO_WINDOW)


def open_in_shell(path: Path) -> None:
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def format_duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


# ----------------------------------------------------------------------------
# 子进程管理：后台线程读取输出，通过队列交回 Tk 主线程，避免跨线程操作界面
# ----------------------------------------------------------------------------
class ManagedProcess:
    def __init__(self, name: str, events: queue.Queue):
        self.name = name
        self.events = events
        self.process: subprocess.Popen[bytes] | None = None
        self.active = False
        self.stop_requested = False
        self.generation = 0

    def start(self, command: list[str], cwd: Path, env: dict[str, str] | None = None) -> None:
        kwargs: dict = {
            "cwd": str(cwd),
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(command, **kwargs)
        self.generation += 1
        self.active = True
        self.stop_requested = False
        threading.Thread(target=self._pump, args=(self.process, self.generation), daemon=True).start()

    def _pump(self, process: subprocess.Popen[bytes], generation: int) -> None:
        assert process.stdout is not None
        try:
            for raw in iter(process.stdout.readline, b""):
                text = ANSI_RE.sub("", decode_output(raw)).rstrip("\r\n").split("\r")[-1]
                self.events.put(("line", self.name, generation, text))
        finally:
            process.stdout.close()
            code = process.wait()
            self.events.put(("exit", self.name, generation, code))

    def finish(self) -> None:
        self.active = False
        self.process = None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process is not None else None

    def terminate_tree(self) -> str | None:
        if self.process is None or self.process.poll() is not None:
            return None
        self.stop_requested = True
        try:
            if IS_WINDOWS:
                subprocess.run(
                    ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                    check=False,
                    capture_output=True,
                    creationflags=CREATE_NO_WINDOW,
                )
            else:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
        except (OSError, ProcessLookupError) as exc:
            return str(exc)
        return None


# ----------------------------------------------------------------------------
# 外观
# ----------------------------------------------------------------------------
def enable_dpi_awareness() -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError, ImportError):
        pass


def setup_fonts(root: tk.Tk) -> dict[str, tkfont.Font]:
    families = set(tkfont.families(root))

    def pick(*candidates: str) -> str | None:
        return next((name for name in candidates if name in families), None)

    ui_family = pick(
        "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Hiragino Sans GB",
        "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei", "Segoe UI",
    ) or tkfont.nametofont("TkDefaultFont").actual("family")
    mono_family = pick("Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono", "Courier New") or "TkFixedFont"
    base = 13 if IS_MAC else 9
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont", "TkTooltipFont"):
        try:
            tkfont.nametofont(name).configure(family=ui_family, size=base)
        except tk.TclError:
            pass
    return {
        "base": tkfont.Font(root, family=ui_family, size=base),
        "bold": tkfont.Font(root, family=ui_family, size=base, weight="bold"),
        "small": tkfont.Font(root, family=ui_family, size=base - 1),
        "small_bold": tkfont.Font(root, family=ui_family, size=base - 1, weight="bold"),
        "card_title": tkfont.Font(root, family=ui_family, size=base + 1, weight="bold"),
        "title": tkfont.Font(root, family=ui_family, size=base + 5, weight="bold"),
        "mono": tkfont.Font(root, family=mono_family, size=base - (1 if IS_MAC else 0)),
    }


def setup_style(root: tk.Tk, fonts: dict[str, tkfont.Font]) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(
        ".",
        background=P["bg"],
        foreground=P["text"],
        font=fonts["base"],
        bordercolor=P["field_border"],
        lightcolor=P["card"],
        darkcolor=P["card"],
        troughcolor=P["bg"],
        focuscolor=P["accent"],
        selectbackground=P["select"],
        selectforeground=P["text"],
        insertcolor=P["text"],
    )
    style.configure("TFrame", background=P["bg"])
    style.configure("Card.TFrame", background=P["card"])
    style.configure("Status.TFrame", background=P["status_bar"])
    style.configure("TLabel", background=P["bg"], foreground=P["text"])
    style.configure("Card.TLabel", background=P["card"])
    style.configure("CardTitle.TLabel", background=P["card"], font=fonts["card_title"])
    style.configure("Muted.TLabel", background=P["card"], foreground=P["muted"], font=fonts["small"])
    style.configure("Dirty.TLabel", background=P["card"], foreground="#d97706", font=fonts["small_bold"])
    style.configure("Toolbar.TLabel", background=P["bg"], foreground=P["muted"], font=fonts["small"])
    style.configure("Status.TLabel", background=P["status_bar"], foreground=P["muted"], font=fonts["small"])

    button_padding = (px(12), px(5))
    style.configure(
        "TButton",
        padding=button_padding,
        background=P["button"],
        foreground=P["text"],
        bordercolor=P["field_border"],
        lightcolor=P["button"],
        darkcolor=P["button"],
        focuscolor=P["button"],
        focusthickness=0,
        anchor="center",
    )
    style.map(
        "TButton",
        background=[("disabled", P["bg"]), ("pressed", P["button_press"]), ("active", P["button_hover"])],
        lightcolor=[("disabled", P["bg"]), ("pressed", P["button_press"]), ("active", P["button_hover"])],
        darkcolor=[("disabled", P["bg"]), ("pressed", P["button_press"]), ("active", P["button_hover"])],
        foreground=[("disabled", P["faint"])],
        bordercolor=[("focus", P["accent"]), ("active", "#b5bfcc")],
    )
    style.configure("Small.TButton", padding=(px(8), px(3)))
    style.configure(
        "Accent.TButton",
        background=P["accent"],
        foreground="#ffffff",
        bordercolor=P["accent"],
        lightcolor=P["accent"],
        darkcolor=P["accent"],
        focuscolor=P["accent"],
        font=fonts["bold"],
        padding=(px(18), px(6)),
    )
    accent_states = [("disabled", P["accent_disabled"]), ("pressed", P["accent_press"]), ("active", P["accent_hover"])]
    style.map(
        "Accent.TButton",
        background=accent_states,
        lightcolor=accent_states,
        darkcolor=accent_states,
        bordercolor=accent_states,
        foreground=[("disabled", "#f1f5ff")],
    )
    style.configure("Danger.TButton", foreground=P["danger"], padding=(px(14), px(6)))
    style.map(
        "Danger.TButton",
        background=[("disabled", P["bg"]), ("pressed", "#f9d6d6"), ("active", P["danger_soft"])],
        lightcolor=[("disabled", P["bg"]), ("pressed", "#f9d6d6"), ("active", P["danger_soft"])],
        darkcolor=[("disabled", P["bg"]), ("pressed", "#f9d6d6"), ("active", P["danger_soft"])],
        foreground=[("disabled", P["faint"])],
        bordercolor=[("disabled", P["field_border"]), ("active", "#f0a3a3"), ("!disabled", "#f3b4b4")],
    )
    style.configure("Tool.TButton", padding=(px(12), px(6)))
    style.configure(
        "TMenubutton",
        padding=(px(12), px(6)),
        background=P["button"],
        bordercolor=P["field_border"],
        lightcolor=P["button"],
        darkcolor=P["button"],
        arrowcolor=P["muted"],
    )
    style.map(
        "TMenubutton",
        background=[("pressed", P["button_press"]), ("active", P["button_hover"])],
        lightcolor=[("pressed", P["button_press"]), ("active", P["button_hover"])],
        darkcolor=[("pressed", P["button_press"]), ("active", P["button_hover"])],
    )

    field_padding = (px(6), px(4))
    for widget in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(
            widget,
            fieldbackground=P["card"],
            background=P["button"],
            bordercolor=P["field_border"],
            lightcolor=P["card"],
            darkcolor=P["card"],
            arrowcolor=P["muted"],
            padding=field_padding,
        )
        style.map(
            widget,
            bordercolor=[("focus", P["accent"]), ("hover", "#aab4c2")],
            lightcolor=[("focus", P["accent"])],
            fieldbackground=[("readonly", P["card"]), ("disabled", P["bg"])],
            foreground=[("disabled", P["faint"])],
        )
    style.configure("Cell.TEntry", padding=(px(4), 0), bordercolor=P["accent"], lightcolor=P["accent"])
    style.configure("TSpinbox", arrowsize=px(11))
    style.map("TCombobox", selectbackground=[("readonly", P["card"])], selectforeground=[("readonly", P["text"])])
    root.option_add("*TCombobox*Listbox.font", fonts["base"])
    root.option_add("*TCombobox*Listbox.selectBackground", P["select"])
    root.option_add("*TCombobox*Listbox.selectForeground", P["text"])
    root.option_add("*TCombobox*Listbox.background", P["card"])

    style.configure(
        "Card.TCheckbutton",
        background=P["card"],
        indicatorbackground=P["card"],
        indicatorforeground="#ffffff",
        upperbordercolor=P["field_border"],
        lowerbordercolor=P["field_border"],
        focuscolor=P["card"],
    )
    style.map(
        "Card.TCheckbutton",
        background=[("active", P["card"])],
        indicatorbackground=[("selected", P["accent"]), ("active", P["accent_soft"])],
        upperbordercolor=[("selected", P["accent"])],
        lowerbordercolor=[("selected", P["accent"])],
    )

    style.configure(
        "Card.TRadiobutton",
        background=P["card"],
        indicatorbackground=P["card"],
        indicatorforeground="#ffffff",
        upperbordercolor=P["field_border"],
        lowerbordercolor=P["field_border"],
        focuscolor=P["card"],
    )
    style.map(
        "Card.TRadiobutton",
        background=[("active", P["card"])],
        indicatorbackground=[("selected", P["accent"]), ("active", P["accent_soft"])],
        upperbordercolor=[("selected", P["accent"])],
        lowerbordercolor=[("selected", P["accent"])],
    )

    row_height = fonts["base"].metrics("linespace") + px(12)
    style.configure(
        "Treeview",
        background=P["card"],
        fieldbackground=P["card"],
        foreground=P["text"],
        rowheight=row_height,
        borderwidth=0,
        relief="flat",
    )
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.map("Treeview", background=[("selected", P["select"])], foreground=[("selected", P["text"])])
    style.configure(
        "Treeview.Heading",
        background=P["stripe"],
        foreground=P["muted"],
        font=fonts["small_bold"],
        relief="flat",
        bordercolor=P["border"],
        lightcolor=P["stripe"],
        darkcolor=P["border"],
        padding=(px(6), px(6)),
    )
    style.map("Treeview.Heading", background=[("active", "#eef1f5")])

    for orient in ("Vertical", "Horizontal"):
        style.configure(
            f"{orient}.TScrollbar",
            background="#d5dae2",
            troughcolor=P["card"],
            bordercolor=P["card"],
            lightcolor="#d5dae2",
            darkcolor="#d5dae2",
            arrowcolor=P["muted"],
            gripcount=0,
            arrowsize=px(12),
        )
        style.map(f"{orient}.TScrollbar", background=[("active", "#b9c0cb")])
    style.configure(
        "Log.Vertical.TScrollbar",
        background="#334155",
        troughcolor=P["log_bg"],
        bordercolor=P["log_bg"],
        lightcolor="#334155",
        darkcolor="#334155",
        arrowcolor="#94a3b8",
    )
    style.map("Log.Vertical.TScrollbar", background=[("active", "#475569")])
    style.configure(
        "Accent.Horizontal.TProgressbar",
        troughcolor="#e2e6ec",
        background=P["accent"],
        bordercolor="#e2e6ec",
        lightcolor=P["accent"],
        darkcolor=P["accent"],
        thickness=px(8),
    )


def make_app_icon(root: tk.Tk) -> tk.PhotoImage:
    size = 32
    image = tk.PhotoImage(master=root, width=size, height=size)
    image.put(P["accent"], to=(0, 0, size, size))
    for x0, height in ((7, 9), (14, 15), (21, 21)):
        image.put("#ffffff", to=(x0, size - 5 - height, x0 + 5, size - 5))
    return image


class Tooltip:
    def __init__(self, widget: tk.Widget, text, delay: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._job: str | None = None
        self._tip: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._job = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._job is not None:
            self.widget.after_cancel(self._job)
            self._job = None

    def _show(self) -> None:
        self._job = None
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        tip = tk.Toplevel(self.widget)
        tip.wm_overrideredirect(True)
        try:
            tip.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        x = self.widget.winfo_rootx() + px(4)
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(4)
        tip.wm_geometry(f"+{x}+{y}")
        tk.Label(
            tip,
            text=text,
            bg="#111827",
            fg="#f9fafb",
            justify="left",
            wraplength=px(520),
            padx=px(9),
            pady=px(6),
            font="TkTooltipFont",
        ).pack()
        self._tip = tip

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


def make_card(parent: tk.Misc, title: str) -> tuple[tk.Frame, ttk.Frame, ttk.Frame]:
    outer = tk.Frame(parent, bg=P["card"], highlightthickness=1, highlightbackground=P["border"], highlightcolor=P["border"], bd=0)
    header = ttk.Frame(outer, style="Card.TFrame", padding=(px(16), px(12), px(16), 0))
    header.pack(fill="x")
    ttk.Label(header, text=title, style="CardTitle.TLabel").pack(side="left")
    body = ttk.Frame(outer, style="Card.TFrame", padding=(px(16), px(8), px(16), px(14)))
    body.pack(fill="both", expand=True)
    return outer, header, body


class ScrollColumn(ttk.Frame):
    """内容超出高度时自动出现滚动条的纵向容器。"""

    def __init__(self, master: tk.Misc):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, bg=P["bg"], highlightthickness=0, bd=0, yscrollincrement=px(24))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.inner = ttk.Frame(self.canvas)
        self._window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self._scrollable = False
        self.inner.bind("<Configure>", self._update, add="+")
        self.canvas.bind("<Configure>", self._update, add="+")
        self.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.bind_all("<Button-4>", self._on_wheel, add="+")
        self.bind_all("<Button-5>", self._on_wheel, add="+")

    def _update(self, _event=None) -> None:
        width = self.canvas.winfo_width()
        self.canvas.itemconfigure(self._window, width=width)
        required = self.inner.winfo_reqheight()
        self.canvas.configure(scrollregion=(0, 0, width, required))
        scrollable = required > self.canvas.winfo_height() + 1
        if scrollable != self._scrollable:
            self._scrollable = scrollable
            if scrollable:
                self.scrollbar.grid(row=0, column=1, sticky="ns", padx=(px(4), 0))
            else:
                self.scrollbar.grid_remove()
                self.canvas.yview_moveto(0)

    def owns(self, widget: tk.Misc | None) -> bool:
        return widget is not None and str(widget).startswith(str(self))

    def _on_wheel(self, event: tk.Event) -> str | None:
        if not self._scrollable:
            return None
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            return None
        if not self.owns(widget):
            return None
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        elif IS_MAC:
            step = -event.delta
        else:
            step = -int(event.delta / 120) or (-1 if event.delta > 0 else 1)
        self.canvas.yview_scroll(step, "units")
        return None


# ----------------------------------------------------------------------------
# 扫描点表格：Treeview + 原位编辑
# ----------------------------------------------------------------------------
class ScanTable(ttk.Frame):
    RUN_STATES = {
        "pending": ("等待", "pending"),
        "running": ("▶ 扫描中", "running"),
        "done": ("✓ 已完成", "done"),
        "stopped": ("■ 已终止", "stopped"),
        "failed": ("✗ 异常退出", "failed"),
    }

    def __init__(self, master: tk.Misc, on_change, fonts: dict[str, tkfont.Font]):
        super().__init__(master, style="Card.TFrame")
        self.on_change = on_change
        self.fonts = fonts
        self.editable = True
        self._editor: dict | None = None
        self._run_state: dict[str, str] = {}

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        border = tk.Frame(self, bg=P["card"], highlightthickness=1, highlightbackground=P["border"], bd=0)
        border.grid(row=0, column=0, sticky="nsew")
        border.columnconfigure(0, weight=1)
        border.rowconfigure(0, weight=1)

        self.tree = ttk.Treeview(border, columns=("idx", "speed", "pressure", "status"), show="headings", selectmode="extended")
        self.tree.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(border, orient="vertical", command=self.tree.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self._on_yscroll)

        for column, text, width, anchor, stretch in (
            ("idx", "#", px(46), "center", False),
            ("speed", "转速 (RPM)", px(120), "center", True),
            ("pressure", "初始背压 (Pa)", px(120), "center", True),
            ("status", "状态", px(140), "w", True),
        ):
            self.tree.heading(column, text=text, anchor=anchor)
            self.tree.column(column, width=width, minwidth=px(40), anchor=anchor, stretch=stretch)

        tree = self.tree
        tree.tag_configure("odd", background=P["stripe"])
        tree.tag_configure("invalid", foreground=P["danger"], background=P["danger_soft"])
        tree.tag_configure("empty", foreground=P["faint"])
        tree.tag_configure("pending", foreground=P["muted"])
        tree.tag_configure("running", foreground=P["accent"], background=P["accent_soft"])
        tree.tag_configure("done", foreground=P["success"])
        tree.tag_configure("stopped", foreground=P["warn"])
        tree.tag_configure("failed", foreground=P["danger"])

        mod = "Command" if IS_MAC else "Control"
        tree.bind("<Double-1>", self._on_double_click)
        tree.bind("<Return>", lambda _e: self._edit_focused())
        tree.bind("<F2>", lambda _e: self._edit_focused())
        tree.bind("<Delete>", lambda _e: self.delete_selected())
        tree.bind("<BackSpace>", lambda _e: self.delete_selected())
        tree.bind("<Insert>", lambda _e: self.add_row())
        tree.bind(f"<{mod}-v>", lambda _e: self._break(self.paste))
        tree.bind(f"<{mod}-c>", lambda _e: self._break(self.copy_selected))
        tree.bind(f"<{mod}-a>", lambda _e: self._break(self.select_all))
        tree.bind("<KeyPress>", self._on_keypress, add="+")
        tree.bind("<Configure>", lambda _e: self._reposition_editor(), add="+")
        tree.bind("<Button-3>", self._show_menu)
        if IS_MAC:
            tree.bind("<Button-2>", self._show_menu)
            tree.bind("<Control-Button-1>", self._show_menu)

        self.menu = tk.Menu(self, tearoff=0)
        accel = "Cmd" if IS_MAC else "Ctrl"
        self.menu.add_command(label="在下方插入行", command=self.add_row, accelerator="Insert")
        self.menu.add_command(label="编辑", command=self._edit_focused, accelerator="Enter")
        self.menu.add_command(label="删除选中行", command=self.delete_selected, accelerator="Delete")
        self.menu.add_separator()
        self.menu.add_command(label="复制", command=self.copy_selected, accelerator=f"{accel}+C")
        self.menu.add_command(label="粘贴（Excel / 文本）", command=self.paste, accelerator=f"{accel}+V")
        self.menu.add_command(label="全选", command=self.select_all, accelerator=f"{accel}+A")
        self.menu.add_separator()
        self.menu.add_command(label="按转速排序", command=self.sort_by_speed)
        self.menu.add_command(label="清空表格", command=self.clear)

    @staticmethod
    def _break(action) -> str:
        action()
        return "break"

    # ---------------- 数据 ----------------
    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        self._cancel_edit()
        self.tree.delete(*self.tree.get_children())
        self._run_state.clear()
        for speed, pressure in rows:
            self.tree.insert("", "end", values=("", speed, pressure, ""))
        self.refresh()

    def get_rows(self) -> list[tuple[str, str]]:
        return [(str(self.tree.set(item, "speed")), str(self.tree.set(item, "pressure"))) for item in self.tree.get_children()]

    def nonempty_items(self) -> list[str]:
        return [
            item
            for item in self.tree.get_children()
            if str(self.tree.set(item, "speed")).strip() or str(self.tree.set(item, "pressure")).strip()
        ]

    def counts(self) -> tuple[int, int]:
        valid = invalid = 0
        for speed, pressure in self.get_rows():
            if not speed.strip() and not pressure.strip():
                continue
            if validate_scan_row(speed, pressure):
                invalid += 1
            else:
                valid += 1
        return valid, invalid

    def refresh(self) -> None:
        for number, item in enumerate(self.tree.get_children()):
            speed = str(self.tree.set(item, "speed")).strip()
            pressure = str(self.tree.set(item, "pressure")).strip()
            run_state = self._run_state.get(item)
            if run_state:
                status, tag = self.RUN_STATES[run_state]
            elif not speed and not pressure:
                status, tag = "空行，将忽略", "empty"
            else:
                error = validate_scan_row(speed, pressure)
                status, tag = (f"✗ {error}", "invalid") if error else ("✓", "")
            tags = [tag] if tag else []
            if number % 2 and tag not in ("invalid", "running"):
                tags.append("odd")
            self.tree.set(item, "idx", number + 1)
            self.tree.set(item, "status", status)
            self.tree.item(item, tags=tags)

    def _changed(self) -> None:
        if self._run_state:
            self._run_state.clear()
        self.refresh()
        self.on_change()

    def commit_pending_edit(self) -> None:
        self._commit_edit()

    def set_editable(self, editable: bool) -> None:
        if not editable:
            self._commit_edit()
        self.editable = editable

    def set_run_states(self, states: dict[str, str]) -> None:
        self._run_state = {item: state for item, state in states.items() if self.tree.exists(item)}
        self.refresh()

    def update_run_state(self, item: str, state: str | None) -> None:
        if not self.tree.exists(item):
            return
        if state is None:
            self._run_state.pop(item, None)
        else:
            self._run_state[item] = state
            if state == "running":
                self.tree.see(item)
        self.refresh()

    def run_states(self) -> dict[str, str]:
        return dict(self._run_state)

    # ---------------- 行操作 ----------------
    def _suggest_speed(self) -> str:
        speeds = []
        for speed, pressure in self.get_rows():
            if not validate_scan_row(speed, pressure):
                speeds.append(int(speed))
        if len(speeds) >= 2 and speeds[-1] > speeds[-2]:
            return str(speeds[-1] + speeds[-1] - speeds[-2])
        return ""

    def add_row(self, speed: str = "", pressure: str = "", edit: bool = True) -> str | None:
        if not self.editable:
            return None
        self._commit_edit()
        children = self.tree.get_children()
        selection = self.tree.selection()
        at_end = not selection or selection[-1] == (children[-1] if children else None)
        if not speed and at_end:
            speed = self._suggest_speed()
        if not pressure and children:
            source = selection[-1] if selection else children[-1]
            pressure = str(self.tree.set(source, "pressure"))
        index = "end" if at_end else self.tree.index(selection[-1]) + 1
        item = self.tree.insert("", index, values=("", speed, pressure, ""))
        self.tree.selection_set(item)
        self.tree.focus(item)
        self.tree.see(item)
        self._changed()
        if edit:
            self.after_idle(lambda: self.begin_edit(item, "speed"))
        return item

    def insert_rows(self, rows: list[tuple[str, str]], replace: bool = False) -> None:
        if not self.editable or not rows:
            return
        self._commit_edit()
        if replace:
            self.tree.delete(*self.tree.get_children())
            index: int | str = "end"
        else:
            selection = self.tree.selection()
            index = self.tree.index(selection[-1]) + 1 if selection else "end"
        new_items = []
        for speed, pressure in rows:
            new_items.append(self.tree.insert("", index, values=("", speed, pressure, "")))
            if index != "end":
                index = int(index) + 1
        self.tree.selection_set(new_items)
        self.tree.see(new_items[-1])
        self._changed()

    def delete_selected(self) -> None:
        if not self.editable:
            return
        self._commit_edit()
        selection = self.tree.selection()
        if not selection:
            return
        children = self.tree.get_children()
        last_index = max(children.index(item) for item in selection)
        survivors = [item for item in children if item not in selection]
        follow = next((item for item in children[last_index + 1:] if item not in selection), survivors[-1] if survivors else None)
        self.tree.delete(*selection)
        if follow:
            self.tree.selection_set(follow)
            self.tree.focus(follow)
        self._changed()

    def move_selected(self, delta: int) -> None:
        if not self.editable:
            return
        self._commit_edit()
        selection = list(self.tree.selection())
        if not selection:
            return
        children = list(self.tree.get_children())
        ordered = sorted(selection, key=children.index, reverse=delta > 0)
        for item in ordered:
            index = self.tree.index(item)
            target = index + delta
            if not 0 <= target < len(children):
                return
            neighbour = self.tree.get_children()[target]
            if neighbour in selection:
                continue
            self.tree.move(item, "", target)
        self.tree.see(ordered[-1])
        self._changed()

    def sort_by_speed(self) -> None:
        if not self.editable:
            return
        rows = self.get_rows()

        def key(row: tuple[str, str]):
            speed = row[0].strip()
            return (0, int(speed)) if SPEED_RE.match(speed) else (1, 0)

        sorted_rows = sorted(rows, key=key)
        if sorted_rows != rows:
            self.set_rows(sorted_rows)
            self.on_change()

    def clear(self) -> None:
        if not self.editable or not self.tree.get_children():
            return
        if messagebox.askyesno("清空表格", "确定要删除全部扫描点吗？", parent=self):
            self.tree.delete(*self.tree.get_children())
            self._changed()

    def select_all(self) -> None:
        self.tree.selection_set(self.tree.get_children())

    def copy_selected(self) -> None:
        items = self.tree.selection() or self.tree.get_children()
        lines = [f"{self.tree.set(item, 'speed')}\t{self.tree.set(item, 'pressure')}" for item in items]
        if lines:
            self.clipboard_clear()
            self.clipboard_append("\n".join(lines))

    def paste(self) -> None:
        if not self.editable:
            return
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        rows = parse_pasted_rows(text)
        if rows:
            self.insert_rows(rows)
        else:
            messagebox.showinfo("粘贴", "剪贴板中没有可识别的“转速, 背压”数据。\n每行两列，可用 Tab、逗号或空格分隔。", parent=self)

    # ---------------- 原位编辑 ----------------
    def _on_double_click(self, event: tk.Event) -> str | None:
        region = self.tree.identify_region(event.x, event.y)
        if region in ("heading", "separator"):
            return None
        item = self.tree.identify_row(event.y)
        if not item:
            self.add_row()
            return "break"
        column = {"#2": "speed", "#3": "pressure"}.get(self.tree.identify_column(event.x), "speed")
        self.begin_edit(item, column)
        return "break"

    def _on_keypress(self, event: tk.Event) -> str | None:
        if event.char and event.char in "0123456789.+-" and not (event.state & 0x4):
            item = self.tree.focus()
            if item and self.editable:
                self.begin_edit(item, "speed", initial=event.char)
                return "break"
        return None

    def _edit_focused(self) -> str:
        item = self.tree.focus() or next(iter(self.tree.selection()), None)
        if item:
            self.begin_edit(item, "speed")
        return "break"

    def begin_edit(self, item: str, column: str, initial: str | None = None) -> None:
        if not self.editable or not self.tree.exists(item):
            return
        self._commit_edit()
        self.tree.see(item)
        self.tree.update_idletasks()
        bbox = self.tree.bbox(item, column)
        if not bbox:
            return
        value = str(self.tree.set(item, column)) if initial is None else initial
        var = tk.StringVar(value=value)
        entry = ttk.Entry(self.tree, textvariable=var, style="Cell.TEntry", justify="center", font=self.fonts["base"])
        x, y, width, height = bbox
        entry.place(x=x, y=y, width=width, height=height)
        entry.focus_set()
        if initial is None:
            entry.select_range(0, "end")
        entry.icursor("end")
        self._editor = {"entry": entry, "item": item, "column": column, "var": var}
        entry.bind("<Return>", lambda _e: self._break(lambda: self._commit_edit("down")))
        entry.bind("<KP_Enter>", lambda _e: self._break(lambda: self._commit_edit("down")))
        entry.bind("<Down>", lambda _e: self._break(lambda: self._commit_edit("down")))
        entry.bind("<Up>", lambda _e: self._break(lambda: self._commit_edit("up")))
        entry.bind("<Tab>", lambda _e: self._break(lambda: self._commit_edit("next")))
        entry.bind("<Shift-Tab>", lambda _e: self._break(lambda: self._commit_edit("prev")))
        entry.bind("<ISO_Left_Tab>", lambda _e: self._break(lambda: self._commit_edit("prev")))
        entry.bind("<Escape>", lambda _e: self._break(self._cancel_edit))
        entry.bind("<FocusOut>", lambda _e: self._commit_edit())

    def _commit_edit(self, move: str | None = None) -> None:
        editor = self._editor
        if editor is None:
            return
        self._editor = None
        value = editor["var"].get().strip()
        item, column = editor["item"], editor["column"]
        editor["entry"].destroy()
        if self.tree.exists(item) and str(self.tree.set(item, column)) != value:
            self.tree.set(item, column, value)
            self._changed()
        if move:
            self._navigate(item, column, move)

    def _cancel_edit(self) -> None:
        editor = self._editor
        if editor is None:
            return
        self._editor = None
        editor["entry"].destroy()
        self.tree.focus_set()

    def _navigate(self, item: str, column: str, move: str) -> None:
        items = self.tree.get_children()
        if item not in items:
            return
        index = items.index(item)
        target: tuple[str, str] | None = None
        if move == "down" and index + 1 < len(items):
            target = (items[index + 1], column)
        elif move == "up" and index > 0:
            target = (items[index - 1], column)
        elif move == "next":
            if column == "speed":
                target = (item, "pressure")
            elif index + 1 < len(items):
                target = (items[index + 1], "speed")
            else:
                self.tree.selection_set(item)
                new_item = self.add_row(edit=False)
                target = (new_item, "speed") if new_item else None
        elif move == "prev":
            if column == "pressure":
                target = (item, "speed")
            elif index > 0:
                target = (items[index - 1], "pressure")
        if target is None:
            self.tree.selection_set(item)
            self.tree.focus(item)
            self.tree.focus_set()
            return
        self.tree.selection_set(target[0])
        self.tree.focus(target[0])
        self.begin_edit(*target)

    def _on_yscroll(self, first: str, last: str) -> None:
        self.scrollbar.set(first, last)
        self._reposition_editor()

    def _reposition_editor(self) -> None:
        editor = self._editor
        if editor is None:
            return
        bbox = self.tree.bbox(editor["item"], editor["column"]) if self.tree.exists(editor["item"]) else ""
        if not bbox:
            self._commit_edit()
            return
        x, y, width, height = bbox
        editor["entry"].place_configure(x=x, y=y, width=width, height=height)

    def _show_menu(self, event: tk.Event) -> None:
        item = self.tree.identify_row(event.y)
        if item and item not in self.tree.selection():
            self.tree.selection_set(item)
            self.tree.focus(item)
        state = "normal" if self.editable else "disabled"
        for label in ("在下方插入行", "编辑", "删除选中行", "粘贴（Excel / 文本）", "按转速排序", "清空表格"):
            self.menu.entryconfigure(label, state=state)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()


class BatchDialog(tk.Toplevel):
    def __init__(self, master: tk.Misc, fonts: dict[str, tkfont.Font], default_pressure: str = "5"):
        super().__init__(master)
        self.withdraw()
        self.title("批量生成扫描点")
        self.configure(bg=P["card"])
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.result: tuple[list[tuple[str, str]], bool] | None = None

        body = ttk.Frame(self, style="Card.TFrame", padding=(px(20), px(16), px(20), px(12)))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="按等间隔转速生成扫描点", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, px(10)))

        self.start_var = tk.StringVar(value="8000")
        self.end_var = tk.StringVar(value="12000")
        self.step_var = tk.StringVar(value="500")
        self.pressure_var = tk.StringVar(value=default_pressure or "5")
        self.mode_var = tk.StringVar(value="replace")
        for row, (label, var) in enumerate(
            (("起始转速 (RPM)", self.start_var), ("终止转速 (RPM)", self.end_var), ("转速步长 (RPM)", self.step_var), ("初始背压 (Pa)", self.pressure_var)),
            start=1,
        ):
            ttk.Label(body, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", pady=px(4), padx=(0, px(14)))
            entry = ttk.Entry(body, textvariable=var, width=16)
            entry.grid(row=row, column=1, sticky="ew", pady=px(4))
            var.trace_add("write", lambda *_: self._update_preview())
            if row == 1:
                entry.focus_set()
                entry.select_range(0, "end")

        modes = ttk.Frame(body, style="Card.TFrame")
        modes.grid(row=5, column=0, columnspan=2, sticky="w", pady=(px(8), 0))
        ttk.Radiobutton(modes, text="替换现有扫描点", value="replace", variable=self.mode_var, style="Card.TRadiobutton").pack(side="left")
        ttk.Radiobutton(modes, text="追加到表格", value="append", variable=self.mode_var, style="Card.TRadiobutton").pack(side="left", padx=(px(16), 0))

        self.preview = ttk.Label(body, style="Muted.TLabel", wraplength=px(330), justify="left")
        self.preview.grid(row=6, column=0, columnspan=2, sticky="w", pady=(px(10), 0))

        buttons = ttk.Frame(self, style="Card.TFrame", padding=(px(20), 0, px(20), px(16)))
        buttons.pack(fill="x")
        ttk.Button(buttons, text="取消", command=self.destroy).pack(side="right")
        self.ok_button = ttk.Button(buttons, text="生成", style="Accent.TButton", command=self._accept)
        self.ok_button.pack(side="right", padx=(0, px(8)))

        self.bind("<Return>", lambda _e: self._accept())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._update_preview()

        self.update_idletasks()
        parent = master.winfo_toplevel()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        self.deiconify()
        self.grab_set()

    def _rows(self) -> list[tuple[str, str]]:
        values = []
        for label, var in (("起始转速", self.start_var), ("终止转速", self.end_var), ("转速步长", self.step_var)):
            text = var.get().strip()
            if not SPEED_RE.match(text) or int(text) <= 0:
                raise ValueError(f"{label}需为正整数")
            values.append(int(text))
        pressure = self.pressure_var.get().strip()
        if not NUMBER_RE.match(pressure) or not math.isfinite(float(pressure)):
            raise ValueError("初始背压需为数字")
        return generate_speed_rows(values[0], values[1], values[2], pressure)

    def _update_preview(self) -> None:
        try:
            rows = self._rows()
        except ValueError as exc:
            self.preview.configure(text=f"✗ {exc}", foreground=P["danger"])
            self.ok_button.state(["disabled"])
            return
        speeds = [row[0] for row in rows]
        shown = "、".join(speeds) if len(speeds) <= 8 else "、".join(speeds[:4]) + " … " + "、".join(speeds[-2:])
        self.preview.configure(text=f"将生成 {len(rows)} 个转速：{shown}", foreground=P["muted"])
        self.ok_button.state(["!disabled"])

    def _accept(self) -> None:
        try:
            rows = self._rows()
        except ValueError:
            return
        self.result = (rows, self.mode_var.get() == "replace")
        self.destroy()


class LogView(ttk.Frame):
    def __init__(self, master: tk.Misc, fonts: dict[str, tkfont.Font]):
        super().__init__(master, style="Card.TFrame")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(
            self,
            wrap="word",
            bg=P["log_bg"],
            fg=P["log_fg"],
            insertbackground=P["log_fg"],
            selectbackground="#334155",
            selectforeground="#ffffff",
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(12),
            pady=px(8),
            font=fonts["mono"],
            spacing1=px(1),
            state="disabled",
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview, style="Log.Vertical.TScrollbar")
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scrollbar.set)
        bold_mono = tkfont.Font(self, font=fonts["mono"])
        bold_mono.configure(weight="bold")
        self.text.tag_configure("ts", foreground="#64748b")
        self.text.tag_configure("error", foreground="#f87171")
        self.text.tag_configure("warn", foreground="#fbbf24")
        self.text.tag_configure("success", foreground="#4ade80")
        self.text.tag_configure("heading", foreground="#7dd3fc", font=bold_mono)
        self.text.tag_configure("dim", foreground="#94a3b8")
        self.text.tag_configure("gui", foreground="#c4b5fd")
        self.text.tag_configure("cmd", foreground="#a5b4fc")
        self._bold_mono = bold_mono

    def append(self, entries: list[tuple[str, str, str | None]]) -> None:
        if not entries:
            return
        follow = self.text.yview()[1] >= 0.999
        self.text.configure(state="normal")
        for stamp, line, tag in entries:
            self.text.insert("end", stamp + "  ", ("ts",), line + "\n", (tag,) if tag else ())
        line_count = int(self.text.index("end-1c").split(".")[0])
        if line_count > MAX_LOG_LINES:
            self.text.delete("1.0", f"{line_count - MAX_LOG_LINES}.0")
        self.text.configure(state="disabled")
        if follow:
            self.text.see("end")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    def get_text(self) -> str:
        return self.text.get("1.0", "end-1c")


class PathField:
    def __init__(self, app: "CfxGui", parent: ttk.Frame, row: int, key: str, label: str, mode: str,
                 filetypes=(), default_ext: str = "", hint: str = "", on_pick=None):
        self.app = app
        self.key = key
        self.label = label
        self.mode = mode
        self.filetypes = list(filetypes) + [("所有文件", "*.*")] if filetypes else [("所有文件", "*.*")]
        self.default_ext = default_ext
        self.hint = hint
        self.on_pick = on_pick
        self.var: tk.StringVar = app.vars[key]
        self.state = ("empty", "")

        ttk.Label(parent, text=label, style="Card.TLabel").grid(row=row, column=0, sticky="w", padx=(0, px(10)), pady=px(3))
        self.entry = ttk.Entry(parent, textvariable=self.var)
        self.entry.grid(row=row, column=1, sticky="ew", pady=px(3))
        self.dot = tk.Label(parent, text="●", bg=P["card"], fg=FIELD_STATE_COLORS["empty"], font=app.fonts["small"], bd=0)
        self.dot.grid(row=row, column=2, padx=(px(6), px(6)))
        ttk.Button(parent, text="浏览…", style="Small.TButton", command=self.browse).grid(row=row, column=3, pady=px(3))
        Tooltip(self.entry, self.tooltip_text)
        Tooltip(self.dot, self.tooltip_text)
        self.entry.bind("<FocusOut>", lambda _e: self.entry.xview_moveto(1.0), add="+")

    def set_state(self, state: str, message: str) -> None:
        self.state = (state, message)
        self.dot.configure(fg=FIELD_STATE_COLORS.get(state, P["muted"]))

    def tooltip_text(self) -> str:
        lines = []
        path = self.app.resolve(self.key)
        if path is not None:
            lines.append(str(path))
        if self.state[1]:
            lines.append(self.state[1])
        if self.hint:
            lines.append(self.hint)
        return "\n".join(lines)

    def _initial_dir(self) -> str:
        path = self.app.resolve(self.key)
        if path is not None:
            if path.is_dir():
                return str(path)
            if path.parent.is_dir():
                return str(path.parent)
        base = self.app.base_dir()
        return str(base if base.is_dir() else ROOT_DIR)

    def browse(self) -> None:
        initial = self._initial_dir()
        if self.mode == "dir":
            selected = filedialog.askdirectory(parent=self.app.root, initialdir=initial, title=f"选择{self.label}")
        elif self.mode == "save":
            current = self.app.resolve(self.key)
            selected = filedialog.asksaveasfilename(
                parent=self.app.root,
                initialdir=initial,
                initialfile=current.name if current else "",
                defaultextension=self.default_ext,
                filetypes=self.filetypes,
                title=f"选择{self.label}",
            )
        else:
            selected = filedialog.askopenfilename(parent=self.app.root, initialdir=initial, filetypes=self.filetypes, title=f"选择{self.label}")
        if selected:
            self.var.set(str(Path(selected)))
            self.entry.xview_moveto(1.0)
            if self.on_pick is not None:
                self.on_pick(Path(selected))


# ----------------------------------------------------------------------------
# 主窗口
# ----------------------------------------------------------------------------
class CfxGui:
    SCAN_FIELDS = ("working_dir", "def_file", "base_ccl", "initial_res", "cfx_bin_dir", "output_csv", "scan_csv")
    FIELD_LABELS = {
        "working_dir": "工作目录",
        "def_file": "DEF 文件",
        "base_ccl": "Base CCL",
        "initial_res": "初场 RES",
        "cfx_bin_dir": "CFX bin 目录",
        "output_csv": "结果 CSV",
        "scan_csv": "扫描 CSV",
        "efficiency_csv": "效率 CSV",
        "plot_output": "特性图",
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.fonts = setup_fonts(root)
        setup_style(root, self.fonts)
        root.title(f"{APP_NAME}  ·  {APP_VERSION}")
        root.configure(bg=P["bg"])
        root.minsize(px(960), px(640))
        self._icon = make_app_icon(root)
        root.iconphoto(True, self._icon)

        self.events: queue.Queue = queue.Queue()
        self.scan_proc = ManagedProcess("scan", self.events)
        self.plot_proc = ManagedProcess("plot", self.events)

        self.vars: dict[str, tk.StringVar] = {
            "working_dir": tk.StringVar(value=str(ROOT_DIR)),
            "def_file": tk.StringVar(),
            "base_ccl": tk.StringVar(),
            "initial_res": tk.StringVar(),
            "cfx_bin_dir": tk.StringVar(),
            "output_csv": tk.StringVar(value="Compressor_Map_Data.csv"),
            "scan_csv": tk.StringVar(value="scan_points.csv"),
            "efficiency_csv": tk.StringVar(value="Extracted_Compressor_Data.csv"),
            "plot_output": tk.StringVar(value="Compressor_Map_With_Efficiency.png"),
            "cores": tk.StringVar(value="8"),
            "blade_count": tk.StringVar(value="1"),
            "flow_unit": tk.StringVar(value="kg/s"),
            "fit_method": tk.StringVar(value="poly_deg_2"),
        }
        self.auto_plot_var = tk.BooleanVar(value=True)
        self.fit_display_var = tk.StringVar(value=FIT_METHODS["poly_deg_2"])
        self.status_var = tk.StringVar(value="就绪")
        self.elapsed_var = tk.StringVar(value="")
        self.progress_var = tk.StringVar(value="")
        self.count_var = tk.StringVar(value="")
        self.dirty_var = tk.StringVar(value="")

        self.fields: dict[str, PathField] = {}
        self.table_dirty = False
        self.temp_scan_csv: Path | None = None
        self.scan_started_at: float | None = None
        self.scan_items: list[str] = []
        self.scan_line_index = 0
        self.scan_point_count = 0
        self._current_speed = ""
        self.plot_auto = False
        self.plot_output_path: Path | None = None
        self._closed = False
        self.plot_tail: deque[str] = deque(maxlen=60)
        self.last_result = ("就绪", "idle")
        self.close_pending = False
        self._save_job: str | None = None
        self._validate_job: str | None = None
        self._timer_job: str | None = None
        self._drain_job: str | None = None
        self._sash_ratios: tuple[float, float] | None = None
        self._zoomed = False

        self._build_ui()
        self._load_settings()
        self._bind_traces()
        self._load_scan_csv(initial=True)
        self._validate_fields()
        self._update_controls()

        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.report_callback_exception = self._report_exception
        mod = "Command" if IS_MAC else "Control"
        root.bind_all(f"<{mod}-s>", lambda _e: self._shortcut(self._save_scan_csv))
        root.bind_all("<F5>", lambda _e: self._shortcut(self._start_scan))
        self._drain_job = root.after(60, self._drain_events)
        root.after(120, self._restore_layout)
        self._log_gui(f"{APP_NAME} {APP_VERSION} 已启动 · 输出解码：UTF-8 / {'、'.join(FALLBACK_ENCODINGS)} 自动识别")

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        self._build_header().grid(row=0, column=0, sticky="ew")
        self._build_toolbar().grid(row=1, column=0, sticky="ew", padx=px(16), pady=(px(12), px(10)))

        pane_options = {"bg": P["bg"], "bd": 0, "sashwidth": px(12), "sashrelief": "flat", "opaqueresize": True}
        self.main_pane = tk.PanedWindow(root, orient="horizontal", **pane_options)
        self.main_pane.grid(row=2, column=0, sticky="nsew", padx=px(16))
        self.main_pane.add(self._build_config_column(self.main_pane), minsize=px(430), stretch="never")
        self.right_pane = tk.PanedWindow(self.main_pane, orient="vertical", **pane_options)
        self.main_pane.add(self.right_pane, minsize=px(420), stretch="always")
        self.right_pane.add(self._build_table_card(self.right_pane), minsize=px(220), stretch="always")
        self.right_pane.add(self._build_log_card(self.right_pane), minsize=px(150), stretch="always")

        self._build_statusbar().grid(row=3, column=0, sticky="ew", pady=(px(10), 0))

    def _build_header(self) -> tk.Frame:
        header = tk.Frame(self.root, bg=P["header"])
        title_box = tk.Frame(header, bg=P["header"])
        title_box.pack(side="left", padx=px(20), pady=px(10))
        tk.Label(title_box, text=APP_NAME, font=self.fonts["title"], bg=P["header"], fg=P["header_text"]).pack(anchor="w")
        tk.Label(
            title_box,
            text=f"ANSYS CFX 自动扫点 · 喘振边界细化 · 特性图绘制      {APP_VERSION}",
            font=self.fonts["small"],
            bg=P["header"],
            fg=P["header_muted"],
        ).pack(anchor="w", pady=(px(2), 0))
        self.status_pill = tk.Label(header, text="● 就绪", font=self.fonts["small_bold"], padx=px(14), pady=px(5), bd=0)
        self.status_pill.pack(side="right", padx=px(20))
        self._apply_pill("就绪", "idle")
        return header

    def _build_toolbar(self) -> ttk.Frame:
        bar = ttk.Frame(self.root)
        self.start_button = ttk.Button(bar, text="▶  开始扫描", style="Accent.TButton", command=self._start_scan)
        self.start_button.pack(side="left")
        Tooltip(self.start_button, "校验参数并调用 cfx.ps1 开始扫点（F5）")
        self.stop_button = ttk.Button(bar, text="■  终止", style="Danger.TButton", command=self._stop_scan)
        self.stop_button.pack(side="left", padx=(px(8), 0))
        Tooltip(self.stop_button, "终止 PowerShell 扫描脚本及其子进程")
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=px(14), pady=px(4))
        self.plot_button = ttk.Button(bar, text="绘制特性图", style="Tool.TButton", command=self._start_plot)
        self.plot_button.pack(side="left")
        Tooltip(self.plot_button, "使用结果 CSV 调用 plot_compressor_map.py 生成特性图")
        open_button = ttk.Menubutton(bar, text="打开")
        open_menu = tk.Menu(open_button, tearoff=0)
        for label, key in (("工作目录", "working_dir"), ("结果 CSV", "output_csv"), ("特性图", "plot_output"), ("扫描 CSV", "scan_csv"), ("效率 CSV", "efficiency_csv")):
            open_menu.add_command(label=label, command=lambda k=key: self._open_path(k))
        open_menu.add_separator()
        open_menu.add_command(label="程序目录（脚本所在位置）", command=lambda: open_in_shell(ROOT_DIR))
        open_button.configure(menu=open_menu)
        open_button.pack(side="left", padx=(px(8), 0))

        progress_box = ttk.Frame(bar)
        progress_box.pack(side="right")
        self.progress = ttk.Progressbar(progress_box, style="Accent.Horizontal.TProgressbar", length=px(220), mode="determinate")
        self.progress.pack(side="right", pady=px(2))
        ttk.Label(progress_box, textvariable=self.progress_var, style="Toolbar.TLabel").pack(side="right", padx=(0, px(10)))
        return bar

    def _build_config_column(self, parent: tk.Misc) -> ScrollColumn:
        scroller = ScrollColumn(parent)
        self.config_column = scroller
        column = scroller.inner
        column.columnconfigure(0, weight=1)

        card, _header, body = make_card(column, "输入文件")
        card.grid(row=0, column=0, sticky="ew")
        body.columnconfigure(1, weight=1)
        specs = (
            ("working_dir", "工作目录", "dir", (), "", "CFX 求解、结果文件所在目录；其余相对路径都以此为基准。", None),
            ("def_file", "DEF 文件", "open", [("DEF 文件", "*.def")], "", "", None),
            ("base_ccl", "Base CCL", "open", [("CCL 文件", "*.ccl")], "", "需包含 MySpeed 与 MyBackPressure 表达式，脚本会逐点替换。", None),
            ("initial_res", "初场 RES", "open", [("RES 文件", "*.res")], "", "可选。首个工况的初始场，留空则从零场启动。", None),
            ("cfx_bin_dir", "CFX bin", "dir", (), "", "可选。找不到 cfx5solve / cfdpost 时填写，例如 C:\\Program Files\\ANSYS Inc\\v251\\CFX\\bin", None),
        )
        for row, (key, label, mode, types, ext, hint, pick) in enumerate(specs):
            self.fields[key] = PathField(self, body, row, key, label, mode, types, ext, hint, pick)

        card, _header, body = make_card(column, "输出文件")
        card.grid(row=1, column=0, sticky="ew", pady=(px(12), 0))
        body.columnconfigure(1, weight=1)
        specs = (
            ("output_csv", "结果 CSV", "save", [("CSV 文件", "*.csv")], ".csv", "扫描结果汇总表，已存在时会复用其中的缓存记录。", None),
            ("scan_csv", "扫描 CSV", "open", [("CSV 文件", "*.csv")], ".csv", "扫描点表格的保存位置；开始扫描时会用表格内容覆盖。留空则使用临时文件。", self._on_scan_csv_picked),
            ("efficiency_csv", "效率 CSV", "open", [("CSV 文件", "*.csv")], ".csv", "可选。绘图时用于补充或插值等熵效率。", None),
            ("plot_output", "特性图", "save", [("PNG 图像", "*.png")], ".png", "压气机特性图输出路径。", None),
        )
        for row, (key, label, mode, types, ext, hint, pick) in enumerate(specs):
            self.fields[key] = PathField(self, body, row, key, label, mode, types, ext, hint, pick)

        card, _header, body = make_card(column, "求解与绘图参数")
        card.grid(row=2, column=0, sticky="ew", pady=(px(12), 0))
        for index in (1, 4):
            body.columnconfigure(index, weight=1)
        body.columnconfigure(2, minsize=px(18))
        cpu_count = os.cpu_count() or 1

        def label(text: str, row: int, column: int) -> None:
            ttk.Label(body, text=text, style="Card.TLabel").grid(row=row, column=column, sticky="w", padx=(0, px(10)), pady=px(4))

        label("CPU 核数", 0, 0)
        cores = ttk.Spinbox(body, from_=1, to=max(256, cpu_count), increment=1, textvariable=self.vars["cores"], width=8)
        cores.grid(row=0, column=1, sticky="w", pady=px(4))
        Tooltip(cores, f"cfx5solve -part 并行分区数（本机 {cpu_count} 个逻辑核）")
        label("叶片数", 0, 3)
        blades = ttk.Spinbox(body, from_=1, to=999, increment=1, textvariable=self.vars["blade_count"], width=8)
        blades.grid(row=0, column=4, sticky="w", pady=px(4))
        Tooltip(blades, "单流道流量 × 叶片数 = 整机流量")
        label("流量单位", 1, 0)
        unit = ttk.Combobox(body, values=FLOW_UNITS, textvariable=self.vars["flow_unit"], state="readonly", width=8)
        unit.grid(row=1, column=1, sticky="w", pady=px(4))
        Tooltip(unit, "CFD-Post 中 massFlow() 返回值的单位")
        label("喘振线拟合", 1, 3)
        fit = ttk.Combobox(body, values=list(FIT_METHODS.values()), textvariable=self.fit_display_var, state="readonly", width=16)
        fit.grid(row=1, column=4, sticky="w", pady=px(4))
        fit.bind("<<ComboboxSelected>>", self._on_fit_selected)
        for widget in (unit, fit):
            widget.bind("<<ComboboxSelected>>", lambda _e, w=widget: w.selection_clear(), add="+")
        ttk.Checkbutton(body, text="扫描成功后自动绘制特性图", variable=self.auto_plot_var, style="Card.TCheckbutton").grid(
            row=2, column=0, columnspan=5, sticky="w", pady=(px(6), 0)
        )
        # 滚轮滚动配置栏时不要误改数值/下拉选项
        for widget in (cores, blades, unit, fit):
            for sequence in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                widget.bind(sequence, lambda e: self._forward_wheel(e), add=False)
        return scroller

    def _build_table_card(self, parent: tk.Misc) -> tk.Frame:
        card, header, body = make_card(parent, "扫描点")
        ttk.Label(header, textvariable=self.count_var, style="Muted.TLabel").pack(side="left", padx=(px(10), 0))
        ttk.Label(header, textvariable=self.dirty_var, style="Dirty.TLabel").pack(side="left", padx=(px(8), 0))
        self.save_table_button = ttk.Button(header, text="保存", style="Small.TButton", command=self._save_scan_csv)
        self.save_table_button.pack(side="right")
        Tooltip(self.save_table_button, f"保存到扫描 CSV（{'Cmd' if IS_MAC else 'Ctrl'}+S）")
        self.load_table_button = ttk.Button(header, text="载入…", style="Small.TButton", command=self._choose_scan_csv)
        self.load_table_button.pack(side="right", padx=(0, px(6)))

        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        tools = ttk.Frame(body, style="Card.TFrame")
        tools.grid(row=0, column=0, sticky="ew", pady=(0, px(8)))
        self.table_buttons = []
        for text, command, tip in (
            ("＋ 添加", lambda: self.table.add_row(), "在选中行下方插入（Insert）"),
            ("批量生成…", self._open_batch_dialog, "按起止转速与步长生成"),
            ("删除", lambda: self.table.delete_selected(), "删除选中行（Delete）"),
            ("上移", lambda: self.table.move_selected(-1), ""),
            ("下移", lambda: self.table.move_selected(1), ""),
        ):
            button = ttk.Button(tools, text=text, style="Small.TButton", command=command)
            button.pack(side="left", padx=(0, px(6)))
            if tip:
                Tooltip(button, tip)
            self.table_buttons.append(button)

        self.table = ScanTable(body, on_change=self._on_table_changed, fonts=self.fonts)
        self.table.grid(row=1, column=0, sticky="nsew")
        mod = "Cmd" if IS_MAC else "Ctrl"
        ttk.Label(
            body,
            text=f"双击或 Enter 编辑 · Tab 跳到下一格 · {mod}+V 从 Excel 粘贴 · 右键更多操作",
            style="Muted.TLabel",
        ).grid(row=2, column=0, sticky="w", pady=(px(8), 0))
        return card

    def _build_log_card(self, parent: tk.Misc) -> tk.Frame:
        card, header, body = make_card(parent, "运行日志")
        body.configure(padding=(px(16), px(8), px(16), px(16)))
        for text, command in (("清空", self._clear_log), ("复制", self._copy_log), ("保存…", self._save_log)):
            ttk.Button(header, text=text, style="Small.TButton", command=command).pack(side="right", padx=(px(6), 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.log = LogView(body, self.fonts)
        self.log.grid(row=0, column=0, sticky="nsew")
        return card

    def _build_statusbar(self) -> ttk.Frame:
        bar = ttk.Frame(self.root, style="Status.TFrame", padding=(px(16), px(5)))
        ttk.Label(bar, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(bar, textvariable=self.elapsed_var, style="Status.TLabel").pack(side="right")
        return bar

    def _forward_wheel(self, event: tk.Event) -> str:
        self.config_column._on_wheel(event)
        return "break"

    def _restore_layout(self) -> None:
        self.root.update_idletasks()
        main_ratio, right_ratio = self._sash_ratios or (0.4, 0.56)
        width = self.main_pane.winfo_width()
        height = self.right_pane.winfo_height()
        if width > 1:
            self.main_pane.sash_place(0, int(width * min(max(main_ratio, 0.3), 0.6)), 0)
        if height > 1:
            self.right_pane.sash_place(0, 0, int(height * min(max(right_ratio, 0.3), 0.8)))

    # ------------------------------------------------------------- settings
    def _bind_traces(self) -> None:
        for key, var in self.vars.items():
            var.trace_add("write", lambda *_: self._schedule_save())
            if key in self.FIELD_LABELS or key in ("cores", "blade_count"):
                var.trace_add("write", lambda *_: self._schedule_validate())
        self.auto_plot_var.trace_add("write", lambda *_: self._schedule_save())

    def _load_settings(self) -> None:
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for key, var in self.vars.items():
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                var.set(value)
        if self.vars["flow_unit"].get() not in FLOW_UNITS:
            self.vars["flow_unit"].set("kg/s")
        if self.vars["fit_method"].get() not in FIT_METHODS:
            self.vars["fit_method"].set("poly_deg_2")
        self.fit_display_var.set(FIT_METHODS[self.vars["fit_method"].get()])
        if isinstance(data.get("auto_plot"), bool):
            self.auto_plot_var.set(data["auto_plot"])
        geometry = data.get("window_geometry")
        if isinstance(geometry, str):
            self._apply_geometry(geometry)
        if data.get("window_zoomed") is True and IS_WINDOWS:
            self._zoomed = True
            self.root.after(50, lambda: self.root.state("zoomed"))
        ratios = data.get("sash_ratios")
        if isinstance(ratios, list) and len(ratios) == 2 and all(isinstance(v, (int, float)) for v in ratios):
            self._sash_ratios = (float(ratios[0]), float(ratios[1]))

    def _apply_geometry(self, geometry: str) -> None:
        match = GEOMETRY_RE.match(geometry.strip())
        if not match:
            return
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        width = min(max(int(match.group(1)), px(960)), screen_w)
        height = min(max(int(match.group(2)), px(640)), screen_h)
        spec = f"{width}x{height}"
        if match.group(3) is not None:
            x, y = int(match.group(3)), int(match.group(4))
            if -width // 2 < x < screen_w - px(100) and 0 <= y < screen_h - px(100):
                spec += f"{match.group(3)}{match.group(4)}"
        self.root.geometry(spec)

    def _schedule_save(self) -> None:
        if self._save_job is not None:
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self._save_settings)

    def _save_settings(self) -> None:
        self._save_job = None
        payload: dict = {key: var.get().strip() for key, var in self.vars.items()}
        payload["auto_plot"] = bool(self.auto_plot_var.get())
        try:
            state = self.root.wm_state()
            payload["window_zoomed"] = state == "zoomed"
            if state == "normal" and self.root.winfo_width() > 200:
                payload["window_geometry"] = self.root.geometry()
            else:
                previous = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.is_file() else {}
                if isinstance(previous, dict) and isinstance(previous.get("window_geometry"), str):
                    payload["window_geometry"] = previous["window_geometry"]
            main_w, right_h = self.main_pane.winfo_width(), self.right_pane.winfo_height()
            if main_w > 1 and right_h > 1:
                payload["sash_ratios"] = [
                    round(self.main_pane.sash_coord(0)[0] / main_w, 4),
                    round(self.right_pane.sash_coord(0)[1] / right_h, 4),
                ]
        except (tk.TclError, OSError, ValueError):
            pass
        try:
            temp_path = SETTINGS_PATH.with_name(SETTINGS_PATH.name + ".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, SETTINGS_PATH)
        except OSError:
            pass

    # ------------------------------------------------------------ helpers
    def base_dir(self) -> Path:
        return resolve_path(self.vars["working_dir"].get(), ROOT_DIR) or ROOT_DIR

    def resolve(self, key: str) -> Path | None:
        if key == "working_dir":
            return resolve_path(self.vars[key].get(), ROOT_DIR)
        return resolve_path(self.vars[key].get(), self.base_dir())

    def _on_fit_selected(self, _event=None) -> None:
        display = self.fit_display_var.get()
        for key, name in FIT_METHODS.items():
            if name == display:
                self.vars["fit_method"].set(key)
                return

    def _log(self, text: str, tag: str | None = None) -> None:
        self.log.append([(datetime.now().strftime("%H:%M:%S"), text, tag)])

    def _log_gui(self, text: str, tag: str = "gui") -> None:
        self._log(text, tag)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _apply_pill(self, text: str, kind: str) -> None:
        bg, fg = PILL_STYLES.get(kind, PILL_STYLES["idle"])
        self.status_pill.configure(text=f"●  {text}", bg=bg, fg=fg)

    def _refresh_pill(self) -> None:
        if self.scan_proc.active:
            if self.scan_proc.stop_requested:
                self._apply_pill("正在终止", "stopping")
            else:
                self._apply_pill("扫描中", "running")
        elif self.plot_proc.active:
            self._apply_pill("绘图中", "plotting")
        else:
            self._apply_pill(*self.last_result)

    def _update_controls(self) -> None:
        scanning = self.scan_proc.active
        self.start_button.state(["disabled"] if scanning else ["!disabled"])
        self.stop_button.state(["!disabled"] if scanning and not self.scan_proc.stop_requested else ["disabled"])
        self.plot_button.state(["disabled"] if self.plot_proc.active else ["!disabled"])
        for button in self.table_buttons + [self.load_table_button]:
            button.state(["disabled"] if scanning else ["!disabled"])
        self.table.set_editable(not scanning)
        self._refresh_pill()

    def _shortcut(self, action) -> str:
        grab = self.root.grab_current()
        if grab is None or grab is self.root:
            action()
        return "break"

    def _report_exception(self, exc_type, exc_value, exc_tb) -> None:
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        self._log_gui(f"界面内部错误：{exc_value}", "error")
        for line in details.rstrip().splitlines():
            self._log(line, "dim")
        messagebox.showerror("内部错误", str(exc_value), detail="详细信息已写入运行日志。", parent=self.root)

    # ---------------------------------------------------------- validation
    def _schedule_validate(self) -> None:
        if self._validate_job is not None:
            self.root.after_cancel(self._validate_job)
        self._validate_job = self.root.after(250, self._validate_fields)

    def _validate_fields(self) -> None:
        self._validate_job = None
        for key, field in self.fields.items():
            field.set_state(*self._check_field(key))

    def _check_field(self, key: str) -> tuple[str, str]:
        path = self.resolve(key)
        if key == "working_dir":
            if path is None:
                return "error", "必填"
            return ("ok", "") if path.is_dir() else ("error", "目录不存在")
        if key in ("def_file", "base_ccl"):
            if path is None:
                return "error", "必填"
            return ("ok", "") if path.is_file() else ("error", "文件不存在")
        if key == "initial_res":
            if path is None:
                return "empty", "未指定：首个工况从零场启动"
            return ("ok", "") if path.is_file() else ("error", "文件不存在")
        if key == "cfx_bin_dir":
            if path is None:
                found = find_executable("cfx5solve")
                if found:
                    return "empty", f"未指定：使用 PATH 中的 {found}"
                return "warn", "未指定，且 PATH 中找不到 cfx5solve"
            if not path.is_dir():
                return "error", "目录不存在"
            missing = [name for name in ("cfx5solve", "cfdpost") if not find_executable(name, path)]
            return ("warn", f"目录中未找到 {' / '.join(missing)}") if missing else ("ok", "")
        if key == "output_csv":
            if path is None:
                return "error", "必填"
            if not path.parent.is_dir():
                return "error", "所在目录不存在"
            return "ok", "已存在：扫描会复用其中的缓存记录" if path.is_file() else "将在扫描时新建"
        if key == "scan_csv":
            if path is None:
                return "empty", "未指定：开始扫描时使用临时文件"
            if not path.parent.is_dir():
                return "error", "所在目录不存在"
            return "ok", "" if path.is_file() else "尚不存在：保存或开始扫描时创建"
        if key == "efficiency_csv":
            if path is None:
                return "empty", "未指定"
            return ("ok", "") if path.is_file() else ("warn", "文件不存在，绘图时将忽略")
        if key == "plot_output":
            if path is None:
                return "error", "必填"
            return ("ok", "") if path.parent.is_dir() else ("error", "所在目录不存在")
        return "ok", ""

    def _parse_common_params(self, errors: list[str]) -> tuple[int | None, float | None]:
        cores_text = self.vars["cores"].get().strip()
        cores = int(cores_text) if cores_text.isdigit() else None
        if cores is None or cores <= 0:
            errors.append("CPU 核数：必须是正整数")
            cores = None
        blade_text = self.vars["blade_count"].get().strip()
        blade_count = float(blade_text) if NUMBER_RE.match(blade_text) else None
        if blade_count is None or not math.isfinite(blade_count) or blade_count <= 0:
            errors.append("叶片数：必须是大于 0 的数字")
            blade_count = None
        return cores, blade_count

    def _collect_scan_config(self) -> tuple[dict, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        for key in self.SCAN_FIELDS:
            state, message = self._check_field(key)
            if state == "error":
                errors.append(f"{self.FIELD_LABELS[key]}：{message}")
            elif state == "warn":
                warnings.append(f"{self.FIELD_LABELS[key]}：{message}")
        cores, blade_count = self._parse_common_params(errors)
        flow_unit = self.vars["flow_unit"].get().strip()
        if flow_unit not in FLOW_UNITS:
            errors.append("流量单位：只能选择 kg/s 或 g/s")
        rows, row_errors = collect_scan_rows(self.table.get_rows())
        errors.extend(row_errors)
        if not SCAN_SCRIPT.is_file():
            errors.append(f"找不到扫描脚本：{SCAN_SCRIPT}")

        cpu_count = os.cpu_count() or 0
        if cores and cpu_count and cores > cpu_count:
            warnings.append(f"CPU 核数 {cores} 超过本机逻辑核数 {cpu_count}")
        base_ccl = self.resolve("base_ccl")
        if base_ccl is not None and base_ccl.is_file():
            has_speed, has_pressure = ccl_has_scan_expressions(base_ccl)
            missing = [name for name, ok in (("MySpeed", has_speed), ("MyBackPressure", has_pressure)) if not ok]
            if missing:
                warnings.append(f"Base CCL 中未找到 {' / '.join(missing)} 表达式，脚本将无法逐点修改转速/背压")
        speeds = [row[0] for row in rows]
        duplicates = sorted({speed for speed in speeds if speeds.count(speed) > 1}, key=int)
        if duplicates:
            warnings.append(f"转速重复：{'、'.join(duplicates)} RPM（后一次会直接复用前一次的结果）")
        max_pressure = read_script_max_pressure()
        if max_pressure is not None:
            too_high = [f"{speed} RPM" for speed, pressure in rows if float(pressure) > max_pressure]
            if too_high:
                warnings.append(f"初始背压超过脚本安全上限 {format_number(max_pressure)} Pa，以下转速线会被直接跳过：{'、'.join(too_high)}")

        config = {
            "working_dir": self.resolve("working_dir"),
            "def_file": self.resolve("def_file"),
            "base_ccl": base_ccl,
            "initial_res": self.resolve("initial_res"),
            "cfx_bin_dir": self.resolve("cfx_bin_dir"),
            "output_csv": self.resolve("output_csv"),
            "scan_csv": self.resolve("scan_csv"),
            "cores": cores,
            "blade_count": blade_count,
            "flow_unit": flow_unit,
            "rows": rows,
        }
        return config, errors, warnings

    def _collect_plot_config(self) -> tuple[dict, list[str], list[str]]:
        errors: list[str] = []
        warnings: list[str] = []
        working_dir = self.resolve("working_dir")
        input_csv = self.resolve("output_csv")
        plot_output = self.resolve("plot_output")
        efficiency_csv = self.resolve("efficiency_csv")
        if working_dir is None or not working_dir.is_dir():
            errors.append("工作目录：目录不存在")
        if input_csv is None or not input_csv.is_file():
            errors.append(f"结果 CSV：文件不存在{f'（{input_csv}）' if input_csv else ''}，请先完成扫描")
        if plot_output is None or not plot_output.parent.is_dir():
            errors.append("特性图：输出目录不存在")
        if not PLOT_SCRIPT.is_file():
            errors.append(f"找不到绘图脚本：{PLOT_SCRIPT}")
        _cores, blade_count = self._parse_common_params([])
        if blade_count is None:
            errors.append("叶片数：必须是大于 0 的数字")
        if efficiency_csv is not None and not efficiency_csv.is_file():
            warnings.append(f"效率 CSV 不存在，将仅使用结果 CSV 中的效率：{efficiency_csv}")
            efficiency_csv = None
        config = {
            "working_dir": working_dir,
            "input_csv": input_csv,
            "plot_output": plot_output,
            "efficiency_csv": efficiency_csv,
            "blade_count": blade_count,
            "fit_method": self.vars["fit_method"].get() if self.vars["fit_method"].get() in FIT_METHODS else "poly_deg_2",
        }
        return config, errors, warnings

    # -------------------------------------------------------- scan table
    def _on_table_changed(self) -> None:
        self._set_table_dirty(True)

    def _set_table_dirty(self, dirty: bool) -> None:
        self.table_dirty = dirty
        valid, invalid = self.table.counts()
        text = f"{valid} 个转速"
        if invalid:
            text += f" · {invalid} 行有误"
        self.count_var.set(text)
        self.dirty_var.set("● 未保存" if dirty else "")

    def _open_batch_dialog(self) -> None:
        rows = self.table.get_rows()
        default_pressure = rows[-1][1] if rows else "5"
        dialog = BatchDialog(self.root, self.fonts, default_pressure)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        new_rows, replace = dialog.result
        if replace and rows and not messagebox.askyesno("替换扫描点", f"将用 {len(new_rows)} 个新扫描点替换现有的 {len(rows)} 行，是否继续？", parent=self.root):
            return
        self.table.insert_rows(new_rows, replace=replace)

    def _on_scan_csv_picked(self, path: Path) -> None:
        self._load_scan_csv(path=path)

    def _choose_scan_csv(self) -> None:
        self.fields["scan_csv"].browse()

    def _load_scan_csv(self, path: Path | None = None, initial: bool = False) -> None:
        if not initial and self.table_dirty:
            if not messagebox.askyesno("载入扫描 CSV", "当前表格有未保存的修改，载入会覆盖这些修改。是否继续？", parent=self.root):
                return
        path = path or self.resolve("scan_csv")
        if path is None or not path.is_file():
            if initial:
                self.table.set_rows(DEFAULT_ROWS)
                self._set_table_dirty(False)
                self._log_gui(f"未找到扫描 CSV，已载入默认扫描点{f'：{path}' if path else ''}")
            else:
                messagebox.showerror("载入失败", f"文件不存在：\n{path}", parent=self.root)
            return
        try:
            rows = read_scan_csv(path)
        except (OSError, ValueError, csv.Error) as exc:
            self._log_gui(f"读取扫描 CSV 失败：{path} · {exc}", "error")
            if initial:
                self.table.set_rows(DEFAULT_ROWS)
                self._set_table_dirty(False)
            else:
                messagebox.showerror("载入失败", f"无法读取扫描 CSV：\n{path}", detail=str(exc), parent=self.root)
            return
        self.table.set_rows(rows)
        self._set_table_dirty(False)
        self._log_gui(f"已载入扫描 CSV（{len(rows)} 行）：{path}")
        if not rows:
            self._log_gui("扫描 CSV 中没有扫描点，请在表格中添加。", "warn")

    def _save_scan_csv(self) -> str:
        if self.scan_proc.active:
            return "break"
        self.table.commit_pending_edit()
        rows, errors = collect_scan_rows(self.table.get_rows())
        if errors:
            messagebox.showerror("无法保存", "扫描点有误，请先修正：", detail="\n".join(errors[:20]), parent=self.root)
            return "break"
        path = self.resolve("scan_csv")
        if path is None:
            selected = filedialog.asksaveasfilename(
                parent=self.root,
                initialdir=str(self.base_dir()),
                initialfile="scan_points.csv",
                defaultextension=".csv",
                filetypes=[("CSV 文件", "*.csv"), ("所有文件", "*.*")],
            )
            if not selected:
                return "break"
            path = Path(selected)
            self.vars["scan_csv"].set(str(path))
        try:
            if not path.parent.is_dir():
                raise OSError(f"目录不存在：{path.parent}")
            write_scan_csv(path, rows)
        except OSError as exc:
            messagebox.showerror("保存失败", f"无法写入扫描 CSV：\n{path}", detail=f"{exc}\n\n如果文件正在 Excel 中打开，请先关闭。", parent=self.root)
            return "break"
        self._set_table_dirty(False)
        self._set_status(f"已保存 {len(rows)} 个扫描点")
        self._log_gui(f"已保存扫描 CSV（{len(rows)} 行）：{path}", "success")
        self._schedule_validate()
        return "break"

    # ------------------------------------------------------------- scan
    def _start_scan(self) -> str:
        if self.scan_proc.active:
            return "break"
        self.table.commit_pending_edit()
        config, errors, warnings = self._collect_scan_config()
        if errors:
            self._set_status("参数校验未通过")
            messagebox.showerror("无法开始扫描", "请先修正以下问题：", detail="\n".join(f"• {item}" for item in errors[:25]), parent=self.root)
            return "break"
        shell = find_powershell()
        if shell is None:
            messagebox.showerror("无法开始扫描", "未找到 PowerShell（powershell 或 pwsh）。", parent=self.root)
            return "break"

        rows = config["rows"]
        speeds = [int(speed) for speed, _ in rows]
        scan_csv: Path | None = config["scan_csv"]
        detail = [
            f"工作目录：{config['working_dir']}",
            f"DEF：{config['def_file'].name}    Base CCL：{config['base_ccl'].name}",
            f"初场：{config['initial_res'].name if config['initial_res'] else '无（从零场启动）'}",
            f"叶片数：{format_number(config['blade_count'])}    流量单位：{config['flow_unit']}",
            f"结果 CSV：{config['output_csv']}",
            f"扫描 CSV：{scan_csv}（将被表格内容覆盖）" if scan_csv else "扫描 CSV：临时文件",
        ]
        if warnings:
            detail.append("")
            detail.append("请注意：")
            detail.extend(f"• {item}" for item in warnings)
        message = f"将扫描 {len(rows)} 条等转速线（{min(speeds)}–{max(speeds)} RPM），并行 {config['cores']} 核。是否开始？"
        if not messagebox.askokcancel("确认开始扫描", message, detail="\n".join(detail), icon="warning" if warnings else "question", parent=self.root):
            return "break"

        try:
            if scan_csv is None:
                fd, name = tempfile.mkstemp(prefix="cfx_scan_points_", suffix=".csv")
                os.close(fd)
                scan_csv = Path(name)
                self.temp_scan_csv = scan_csv
            write_scan_csv(scan_csv, rows)
        except OSError as exc:
            self._cleanup_temp_csv()
            messagebox.showerror("无法写入扫描 CSV", str(scan_csv), detail=f"{exc}\n\n如果文件正在 Excel 中打开，请先关闭。", parent=self.root)
            return "break"
        if config["scan_csv"] is not None:
            self._set_table_dirty(False)

        command = build_scan_command(shell, config, scan_csv)
        self._log("=" * 60, "heading")
        self._log_gui(f"开始扫描：{len(rows)} 条等转速线 · {config['cores']} 核")
        self._log(subprocess.list2cmdline(command), "cmd")
        if config["cfx_bin_dir"]:
            self._log_gui(f"CFX bin 目录已加入本次扫描 PATH：{config['cfx_bin_dir']}")
        for item in warnings:
            self._log_gui(f"注意：{item}", "warn")
        try:
            self.scan_proc.start(command, cwd=config["working_dir"], env=build_scan_environment(config["cfx_bin_dir"]))
        except OSError as exc:
            self._cleanup_temp_csv()
            self._log_gui(f"扫描启动失败：{exc}", "error")
            self.last_result = ("启动失败", "error")
            self._update_controls()
            messagebox.showerror("启动失败", f"无法启动扫描脚本：{exc}", parent=self.root)
            return "break"

        self.scan_items = self.table.nonempty_items()
        self.scan_line_index = 0
        self.scan_point_count = 0
        self._current_speed = ""
        self.table.set_run_states({item: "pending" for item in self.scan_items})
        self.progress.configure(maximum=max(len(self.scan_items), 1), value=0)
        self.progress_var.set(f"等待第 1/{len(self.scan_items)} 条转速线")
        self.scan_started_at = time.monotonic()
        self._tick()
        self._set_status(f"扫描进行中 · PID {self.scan_proc.pid}")
        self._update_controls()
        return "break"

    def _stop_scan(self, confirm: bool = True) -> None:
        if not self.scan_proc.active or self.scan_proc.stop_requested:
            return
        if confirm and not messagebox.askyesno("终止扫描", "确定要终止当前扫描吗？\nPowerShell 脚本及其启动的 CFX 进程都会被结束。", icon="warning", parent=self.root):
            return
        self._log_gui(f"正在终止扫描进程树（PID {self.scan_proc.pid}）…", "warn")
        error = self.scan_proc.terminate_tree()
        if error:
            self._log_gui(f"终止进程失败：{error}", "error")
        self._set_status("正在终止扫描…")
        self._update_controls()

    def _on_scan_line(self, text: str) -> None:
        match = SPEED_LINE_RE.search(text)
        if match:
            if self.scan_line_index > 0 and self.scan_line_index - 1 < len(self.scan_items):
                self.table.update_run_state(self.scan_items[self.scan_line_index - 1], "done")
            if self.scan_line_index < len(self.scan_items):
                self.table.update_run_state(self.scan_items[self.scan_line_index], "running")
            self.scan_line_index += 1
            self.progress.configure(value=self.scan_line_index - 1)
            self._update_progress_text(match.group(1))
        elif POINT_RE.search(text):
            self.scan_point_count += 1
            self._update_progress_text()

    def _update_progress_text(self, speed: str | None = None) -> None:
        if speed is not None:
            self._current_speed = speed
        self.progress_var.set(
            f"转速线 {self.scan_line_index}/{len(self.scan_items)} · {self._current_speed} RPM · 已提取 {self.scan_point_count} 个工况点"
        )

    def _on_scan_exit(self, code: int) -> None:
        stopped = self.scan_proc.stop_requested
        self.scan_proc.finish()
        self._cleanup_temp_csv()
        elapsed = format_duration(time.monotonic() - self.scan_started_at) if self.scan_started_at else ""
        self.scan_started_at = None
        if self._timer_job is not None:
            self.root.after_cancel(self._timer_job)
            self._timer_job = None
        self.elapsed_var.set(f"上次扫描耗时 {elapsed}" if elapsed else "")

        for item, state in self.table.run_states().items():
            if state == "running":
                self.table.update_run_state(item, "stopped" if stopped else ("done" if code == 0 else "failed"))
            elif state == "pending":
                self.table.update_run_state(item, None)
        if code == 0 and not stopped:
            self.progress.configure(value=self.progress.cget("maximum"))
        if stopped:
            self.last_result = ("已终止", "stopped")
        elif code == 0:
            self.last_result = ("扫描完成", "success")
        else:
            self.last_result = (f"异常退出 ({code})", "error")
        self._update_controls()

        if stopped:
            self._set_status(f"扫描已终止 · 耗时 {elapsed}")
            self._log_gui(f"扫描已终止（退出码 {code}）", "warn")
            self._offer_kill_residual()
            if self.close_pending:
                self._shutdown()
                return
            messagebox.showinfo("已终止", "扫描脚本及其子进程已终止。", parent=self.root)
            return

        if code == 0:
            self._set_status(f"扫描完成 · {self.scan_point_count} 个工况点 · 耗时 {elapsed}")
            self._log_gui(f"扫描完成（耗时 {elapsed}）", "success")
            self.root.bell()
            if self.auto_plot_var.get() and self._start_plot(auto=True):
                self._log_gui("已自动启动绘图。")
            else:
                messagebox.showinfo("扫描完成", f"扫描脚本已执行完成。\n共提取 {self.scan_point_count} 个工况点，耗时 {elapsed}。", parent=self.root)
            return

        self._set_status(f"扫描异常退出，退出码 {code}")
        self._log_gui(f"扫描脚本异常退出，退出码 {code}", "error")
        self.root.bell()
        messagebox.showwarning("扫描异常结束", f"扫描脚本已退出，退出码 {code}。", detail="请查看运行日志中的错误信息。", parent=self.root)

    def _offer_kill_residual(self) -> None:
        residual = find_cfx_processes()
        if not residual:
            return
        names: dict[str, int] = {}
        for name, _pid in residual:
            names[name] = names.get(name, 0) + 1
        summary = "、".join(f"{name} ×{count}" for name, count in names.items())
        self._log_gui(f"检测到仍在运行的 CFX 进程：{summary}", "warn")
        if messagebox.askyesno(
            "残留 CFX 进程",
            f"检测到仍在运行的 CFX 进程：\n{summary}\n\n是否强制结束？",
            detail="这些进程可能不在脚本进程树内（例如 MPI 派生的求解器）。如果本机同时在运行其他 CFX 计算，它们也会被结束。",
            icon="warning",
            parent=self.root,
        ):
            kill_pids([pid for _name, pid in residual])
            self._log_gui("已强制结束残留 CFX 进程。", "warn")

    def _cleanup_temp_csv(self) -> None:
        if self.temp_scan_csv is not None:
            try:
                self.temp_scan_csv.unlink(missing_ok=True)
            except OSError:
                pass
            self.temp_scan_csv = None

    def _tick(self) -> None:
        if self.scan_started_at is None:
            return
        self.elapsed_var.set(f"已运行 {format_duration(time.monotonic() - self.scan_started_at)}")
        self._timer_job = self.root.after(1000, self._tick)

    # ------------------------------------------------------------- plot
    def _start_plot(self, auto: bool = False) -> bool:
        if self.plot_proc.active:
            if not auto:
                messagebox.showinfo("绘图进行中", "绘图任务正在运行，请稍候。", parent=self.root)
            return False
        config, errors, warnings = self._collect_plot_config()
        if errors:
            self._log_gui("无法绘图：" + "；".join(errors), "error")
            if not auto:
                messagebox.showerror("无法绘图", "请先修正以下问题：", detail="\n".join(f"• {item}" for item in errors), parent=self.root)
            return False
        for item in warnings:
            self._log_gui(item, "warn")
        command = build_plot_command(config)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._log_gui("启动绘图：")
        self._log(subprocess.list2cmdline(command), "cmd")
        try:
            self.plot_proc.start(command, cwd=config["working_dir"], env=env)
        except OSError as exc:
            self._log_gui(f"绘图启动失败：{exc}", "error")
            if not auto:
                messagebox.showerror("启动失败", f"无法启动绘图脚本：{exc}", parent=self.root)
            return False
        self.plot_auto = auto
        self.plot_output_path = config["plot_output"]
        self.plot_tail.clear()
        if not self.scan_proc.active:
            self._set_status("正在绘制特性图…")
        self._update_controls()
        return True

    def _on_plot_exit(self, code: int) -> None:
        self.plot_proc.finish()
        output = self.plot_output_path
        auto = self.plot_auto
        if code == 0:
            if not self.scan_proc.active:
                self.last_result = ("特性图已生成", "success")
                self._set_status(f"特性图已生成：{output}")
            self._log_gui(f"特性图已生成：{output}", "success")
            self._update_controls()
            prefix = "扫描完成，" if auto else ""
            if output is not None and messagebox.askyesno("绘图完成", f"{prefix}压气机特性图已生成，是否立即打开？", detail=str(output), parent=self.root):
                try:
                    open_in_shell(output)
                except OSError as exc:
                    messagebox.showerror("无法打开", str(exc), parent=self.root)
            return
        hint = ""
        tail = "\n".join(self.plot_tail)
        if "No module named" in tail:
            hint = "缺少 Python 依赖，请在命令行执行：\npip install matplotlib numpy"
        if not self.scan_proc.active:
            self.last_result = ("绘图失败", "error")
            self._set_status(f"绘图失败，退出码 {code}")
        self._log_gui(f"绘图脚本退出，退出码 {code}", "error")
        self._update_controls()
        title = "自动绘图失败" if auto else "绘图失败"
        messagebox.showwarning(title, f"绘图脚本退出，退出码 {code}。", detail=hint or "请查看运行日志中的错误信息。", parent=self.root)

    # ----------------------------------------------------------- events
    def _drain_events(self) -> None:
        if self._closed:
            return
        entries: list[tuple[str, str, str | None]] = []
        exits: list[tuple[str, int]] = []
        stamp = datetime.now().strftime("%H:%M:%S")
        try:
            for _ in range(2000):
                kind, name, generation, payload = self.events.get_nowait()
                proc = self.scan_proc if name == "scan" else self.plot_proc
                if generation != proc.generation:
                    continue
                if kind == "line":
                    text = str(payload)
                    if not text.strip() or text.startswith(("#< CLIXML", "<Objs Version=")):
                        continue
                    entries.append((stamp, text, classify_log_line(text)))
                    if name == "scan":
                        self._on_scan_line(text)
                    else:
                        self.plot_tail.append(text)
                else:
                    exits.append((name, int(payload)))
        except queue.Empty:
            pass
        self.log.append(entries)
        self._drain_job = self.root.after(80 if entries else 120, self._drain_events)
        for name, code in exits:
            if self._closed:
                return
            if name == "scan":
                self._on_scan_exit(code)
            else:
                self._on_plot_exit(code)

    # ------------------------------------------------------------ misc
    def _open_path(self, key: str) -> None:
        path = self.resolve(key)
        if path is None or not path.exists():
            messagebox.showinfo("无法打开", f"{self.FIELD_LABELS.get(key, key)} 不存在：\n{path or '未指定'}", parent=self.root)
            return
        try:
            open_in_shell(path)
        except OSError as exc:
            messagebox.showerror("无法打开", str(exc), parent=self.root)

    def _clear_log(self) -> None:
        self.log.clear()

    def _copy_log(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get_text())
        self._set_status("日志已复制到剪贴板")

    def _save_log(self) -> None:
        selected = filedialog.asksaveasfilename(
            parent=self.root,
            initialdir=str(self.base_dir()),
            initialfile=f"cfx_scan_log_{datetime.now():%Y%m%d_%H%M%S}.txt",
            defaultextension=".txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            Path(selected).write_text(self.log.get_text(), encoding="utf-8")
        except OSError as exc:
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return
        self._set_status(f"日志已保存：{selected}")

    def _on_close(self) -> None:
        if self.scan_proc.active:
            if self.close_pending:
                if messagebox.askyesno("强制关闭", "扫描进程仍未退出，是否直接关闭窗口？", parent=self.root):
                    self._shutdown()
                return
            if not messagebox.askyesno("退出", "扫描仍在运行。关闭窗口会终止 PowerShell 脚本及其子进程，是否继续？", icon="warning", parent=self.root):
                return
            self.close_pending = True
            self._stop_scan(confirm=False)
            return
        if self.table_dirty:
            answer = messagebox.askyesnocancel("退出", "扫描点表格有未保存的修改，是否先保存？", parent=self.root)
            if answer is None:
                return
            if answer:
                self._save_scan_csv()
                if self.table_dirty:
                    return
        self._shutdown()

    def _shutdown(self) -> None:
        for job in (self._save_job, self._validate_job, self._timer_job, self._drain_job):
            if job is not None:
                self.root.after_cancel(job)
        self._save_settings()
        if self.plot_proc.active:
            self.plot_proc.terminate_tree()
        self._cleanup_temp_csv()
        self._closed = True
        self.root.destroy()


def main() -> None:
    global UI_SCALE
    enable_dpi_awareness()
    root = tk.Tk()
    root.withdraw()
    UI_SCALE = max(1.0, root.winfo_fpixels("1i") / (72.0 if IS_MAC else 96.0))
    root.geometry(f"{px(1240)}x{px(860)}")
    CfxGui(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
