# CFX 特性曲线扫描工具

本仓库用于执行 ANSYS CFX 压缩机特性曲线扫点，并提供一个可在 Windows 下启动的中文 GUI 界面来配置扫描参数。

## 主要文件

- [cfx.ps1](/Users/hao/code/workingmap/cfx.ps1)：主扫描脚本，负责读取扫描配置并执行 CFX 求解与后处理。
- [cfx_gui.py](/Users/hao/code/workingmap/cfx_gui.py)：中文图形界面，版本号为 `Version 1.0`。
- [plot_compressor_map.py](/Users/hao/code/workingmap/plot_compressor_map.py)：压气机特性图绘制脚本，可由 GUI 自动或手动执行。
- [start_cfx_gui.bat](/Users/hao/code/workingmap/start_cfx_gui.bat)：Windows 下一键启动 GUI。
- [scan_points.csv](/Users/hao/code/workingmap/scan_points.csv)：默认扫描点配置文件。

说明：
- 仓库当前不再内置 `base.def` 和 `base.ccl` 模板文件。
- 启动 GUI 或直接运行 `cfx.ps1` 前，需要自行提供有效的 `.def` 和 `.ccl` 文件路径。
- 如需指定求解初场，可额外提供可选的 `.res` 文件路径。

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
- 首次启动后，请在 GUI 中手动选择要使用的 `DEF` 与 `Base CCL` 文件
- 如需从指定初场启动，可在 GUI 中选择可选的 `RES` 文件
- 如果系统找不到 `cfx5solve` 或 `cfdpost`，可在 GUI 中填写 ANSYS CFX 的 `bin` 目录，程序会在本次扫描进程中临时加入 `PATH`
- GUI 会记住上次填写的路径、核数、叶片数、流量单位、绘图路径、自动绘图选项以及窗口大小，保存在仓库目录下的 `.cfx_gui_settings.json`
- GUI 提供“终止扫描”按钮；关闭窗口时若任务仍在运行，会先终止 PowerShell 脚本及其子进程
- GUI 提供“绘制压气机图”按钮，可直接调用 `plot_compressor_map.py`；默认勾选“扫描完成后自动绘图”，扫描成功后会自动生成 PNG 图像
- 绘图时会读取结果 CSV 中的 `Blade_Count`；新结果不会重复乘叶片数，旧结果缺少该列时会按 GUI 中的叶片数从单流道流量换算
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
-InitialResFile <string>
-CsvFile <string>
-SpeedPressureTablePath <string>
-Cores <int>
-BladeCount <double>
-MassFlowUnit <string>
-WorkingDirectory <string>
```

说明：

- `BladeCount` 用于把后处理读取到的单流道质量流量换算成整机流量。
- `MassFlowUnit` 由用户显式选择，目前支持 `kg/s` 和 `g/s`。
- 当前实现不再依赖从 `.res` 自动猜流量单位或叶片数，而是要求用户显式输入，稳定性更高。

## 说明

- `.gitignore` 已默认忽略常见结果文件、缓存文件和大体积输出。
- 当前仓库保留核心脚本与 GUI 文件，模板输入文件需由使用方自行提供。
