# CFX 特性曲线扫描工具

本仓库用于执行 ANSYS CFX 压缩机特性曲线扫点，并提供一个可在 Windows 下启动的中文 GUI 界面来配置扫描参数。

## 主要文件

- [cfx.ps1](cfx.ps1)：主扫描脚本，负责读取扫描配置并执行 CFX 求解与后处理。
- [cfx_gui.py](cfx_gui.py)：中文图形界面，版本号为 `Version 2.0`。
- [plot_compressor_map.py](plot_compressor_map.py)：压气机特性图绘制脚本，可由 GUI 自动或手动执行。
- [start_cfx_gui.bat](start_cfx_gui.bat)：Windows 下一键启动 GUI。
- [scan_points.csv](scan_points.csv)：默认扫描点配置文件。
- [data_only.ps1](data_only.ps1)：对当前目录下全部 `.res` 批量提取到 `Extracted_Compressor_Data.csv`（可作为绘图的效率 CSV）。增量执行：已提取且 `.res` 未变化的结果会跳过，重新提取时覆盖同名记录，不会产生重复行；加 `-Force` 可强制全部重新提取。

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
- 结果 CSV 会保存 `.res` 的修改时间与文件大小作为缓存指纹；同名 `.res` 被重新求解或覆盖后，主脚本会自动重新后处理并更新原记录
- 一旦发现流量骤降、锁墙或发散点，该压力会成为失败上界；后续只在“最后稳定点—最低失败上界”之间二分细化，不再读取区间外的更高历史工况，也不会放宽已经确认的失败上界
- 目标环境中可调用 PowerShell 和 ANSYS CFX 相关命令

## GUI 使用要点（Version 2.0）

- 左侧为输入/输出文件与参数，每个路径后的圆点表示校验状态：绿色正常、黄色提醒、红色错误、灰色未填（可选项），悬停可查看完整路径与说明
- 相对路径统一以“工作目录”为基准解析；从资源管理器“复制为路径”粘贴的带引号路径也能识别
- 扫描点表格：双击或 Enter 编辑，Tab / Shift+Tab 在单元格间跳转（末行 Tab 自动新增一行），可直接从 Excel 粘贴两列数据，右键可排序、清空；“批量生成…”按起止转速与步长生成扫描点；无效行以红色标出
- `Ctrl+S` 保存扫描 CSV，`F5` 开始扫描；开始前会弹出确认框，汇总扫描范围及潜在问题（如找不到 cfx5solve、Base CCL 缺少 MySpeed/MyBackPressure、初始背压超过脚本安全上限、转速重复）
- 扫描中表格会逐行显示“扫描中 / 已完成”，工具栏显示当前转速线、已提取工况点数，状态栏显示已运行时间；运行日志按错误/警告/成功着色并带时间戳，可复制或保存
- 终止扫描后，若检测到未随进程树退出的 `solver-mpi.exe` 等 CFX 进程，会询问是否一并结束
- 绘图可选择喘振线拟合方式（二次 / 一次多项式 / 自动），完成后可直接打开图像；“打开”菜单可快速打开工作目录、结果 CSV、特性图等
- 窗口大小、分栏位置与各项参数会自动保存

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
