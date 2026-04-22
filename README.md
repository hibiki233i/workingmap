# CFX 特性曲线扫描工具

本仓库用于执行 ANSYS CFX 压缩机特性曲线扫点，并提供一个可在 Windows 下启动的中文 GUI 界面来配置扫描参数。

## 主要文件

- [cfx.ps1](/Users/hao/code/workingmap/cfx.ps1)：主扫描脚本，负责读取扫描配置并执行 CFX 求解与后处理。
- [cfx_gui.py](/Users/hao/code/workingmap/cfx_gui.py)：中文图形界面，版本号为 `Version 1.0`。
- [start_cfx_gui.bat](/Users/hao/code/workingmap/start_cfx_gui.bat)：Windows 下一键启动 GUI。
- [scan_points.csv](/Users/hao/code/workingmap/scan_points.csv)：默认扫描点配置文件。
- [base.def](/Users/hao/code/workingmap/base.def)：默认 `def` 输入文件。
- [base.ccl](/Users/hao/code/workingmap/base.ccl)：默认基础 `ccl` 文件。

## GUI 启动

在 Windows 下，双击：

```bat
start_cfx_gui.bat
```

或命令行执行：

```bat
py -3 cfx_gui.py
```

要求：

- 已安装 Python 3
- 启动脚本会依次尝试 `py`、`python`、`python3`
- 如果 `PATH` 中没有 Python，启动脚本还会继续探测这些常见安装目录：
  - `%LocalAppData%\Programs\Python\Python*`
  - `%ProgramFiles%\Python\Python*`
  - `%ProgramFiles(x86)%\Python\Python*`
- 目标环境中可调用 PowerShell 和 ANSYS CFX 相关命令

如果仍提示找不到 Python，优先检查 Windows 安装器是否勾选了 `Add python.exe to PATH`，然后重新打开终端或重新双击 `start_cfx_gui.bat`。

## 扫描配置格式

扫描配置使用 CSV，表头固定为：

```csv
SpeedRPM,InitialPressurePa
```

示例：

```csv
SpeedRPM,InitialPressurePa
8000,5
8500,5
9000,5
```

每一行代表一个扫描转速点及其初始压力。GUI 中的“扫描数量”即表格行数。

## 脚本参数

`cfx.ps1` 支持以下入口参数：

```powershell
-DefFile <string>
-BaseCclFile <string>
-CsvFile <string>
-SpeedPressureTablePath <string>
-Cores <int>
-WorkingDirectory <string>
```

## 说明

- `.gitignore` 已默认忽略常见结果文件、缓存文件和大体积输出。
- 当前仓库保留核心输入模板与脚本文件，便于版本管理。
