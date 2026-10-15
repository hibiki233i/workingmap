# workingmap 数据格式（schema_version = 1）

本约定连接「优化结果 CSV → 性能评估 → 特性图」。示例均为**合成数据**，用于离线验证，不代表压缩机测量或 CFD 结果。

## 扫描输入：scan_points.csv

UTF-8 CSV（允许 BOM），逗号分隔，小数点使用 `.`。一行代表一条等转速线的起始工况。

| 字段 | 含义与单位 |
| --- | --- |
| `SpeedRPM` | 转速，正整数，rpm |
| `InitialPressurePa` | 起始出口背压，Pa，有限数；脚本写入 `MyBackPressure` |

示例：`examples/scan_points.csv`。背压是 CCL 边界条件的设定值，保留模型本身的参考压力约定；不是测得的出口绝对压力。现有扫描脚本的安全上限为 16 Pa，具体模型应检查边界和参考压力设置。

## 特性图数据

文件名任意（通常为 `Compressor_Map_Data.csv` 或 `Extracted_Compressor_Data.csv`）。列顺序不限，新写入的完整记录使用版本 `1`。空值表示未知，不能填零代替缺失值。绘图需要 `Mass_Flow_kg_s`、所选压比及转速/背压；其余物理量可按来源提供。

| 字段 | 含义与单位 |
| --- | --- |
| `schema_version` | `1`；无列/空值为旧格式或未补齐的旧记录 |
| `Result_File` | 来源标识（一般为 `.res` 文件名）；显式工况列齐全时可省略。效率表直接匹配使用非空来源标识、转速和背压 |
| `Speed_RPM` | 工况转速，rpm，正有限数；优先于文件名 |
| `Back_Pressure_Pa` | 工况出口背压设定值，Pa，有限数；优先于文件名 |
| `Mass_Flow_kg_s` | **始终为 kg/s**，正向出口流量，已按 `Blade_Count` 缩放 |
| `Blade_Count` | 本行流量所代表的流道倍数，正数；1 表示单流道，10 表示单流道 × 10 |
| `Mass_Flow_Unit` | CFD-Post 原始输出的单位，`kg/s` 或 `g/s`；是提取配置记录，**不是 `Mass_Flow_kg_s` 的单位** |
| `Static_PR` | 无量纲，出口/进口质量流量平均静压比 |
| `Total_PR` | 无量纲，静止系出口/进口质量流量平均总压比 |
| `P_in_Pa`, `P_out_Pa` | 质量流量平均进口/出口静压，Pa，沿用 CFD-Post `Pressure` 的参考约定 |
| `T_in_K`, `T_out_K` | 质量流量平均进口/出口静温，K |
| `Isentropic_Efficiency` | CFD-Post 出口平均等熵压缩效率，提取值通常为 0–1；绘图兼容旧表的百分数（大于 1 视为 %） |
| `Result_LastWriteUtcTicks` | 可选缓存指纹，`.res` 的 UTC 修改时间，.NET ticks（100 ns，自公元 1 年起） |
| `Result_Size_Bytes` | 可选缓存指纹，`.res` 大小，byte |

`Static_PR` 的 CEL 定义：

```text
massFlowAve(Pressure)@R1 Outlet / massFlowAve(Pressure)@R1 Inlet
```

`Total_PR` 与叶轮优化仓库 `cfx_post/Extract_Results.cse` 一致：

```text
massFlowAve(Total Pressure in Stn Frame)@R1 Outlet
/ massFlowAve(Total Pressure in Stn Frame)@R1 Inlet
```

两个平均值分别计算后相除，不使用旋转系总压，不以静压比替代总压比。入口平均压力为零时提取宏写入 0；绘图跳过无效压比并报告文件与行号。边界名必须与 `.res` 中的 `R1 Inlet`、`R1 Outlet` 一致。

### 流量换算

提取端：`Mass_Flow_kg_s = 原始 -massFlow()@R1 Outlet × 原始单位换算因子 × Blade_Count`，g/s 因子为 0.001，kg/s 为 1。

绘图端：`图上 g/s = Mass_Flow_kg_s × --blade-count / Blade_Count × 1000`。
例如 CSV 记录 0.002 kg/s、`Blade_Count=10`，以 `--blade-count 10` 绘图为 2 g/s；`--blade-count 20` 为 4 g/s。即使 `Mass_Flow_Unit=g/s`，也不会再次做原始单位换算。

### 旧格式与迁移

- `Speed_RPM` 或 `Back_Pressure_Pa` 缺列/空值时，逐字段从 `Result_File` 的 `Map_Speed_<整数>_Press_<数值>` 回退，并发出警告。显式的非法数值不会被文件名覆盖。
- 缺失 `Blade_Count` 时警告并按单流道处理；未知单位的旧数据应先确认 `Mass_Flow_kg_s` 数值确实是 kg/s。
- 无效流量、压比、工况、叶片数会跳过并报告行号；无有效行时命令失败。未知版本会报错。元数据补齐和 CSV 重写保留已有非空 `schema_version`；只对版本为空且工况/总压比字段齐全的记录补写 `1`。
- `--pr-type total` 要求 `Total_PR`；不会回退为静压比。旧数据要先重新提取或补齐总压比。
- `cfx.ps1` 读取到与规范列头不一致的表时备份为 `.legacy_upgrade_<时间>.bak`，保留旧行及未知空值，并从文件名补齐可识别工况；重写后只保留规范列，用户自加的列（如备注）仅留在备份中。缺失或无效总压比的缓存重新后处理，不重复运行求解器。
- `data_only.ps1` 同样将缺失或无效总压比的旧缓存重新提取并覆盖同名记录；未找到对应 `.res` 的旧行保留未知空值。不齐全的旧行不标成版本 1。
- 缓存要求总压比可解析为有限正数，并检查叶片数、原始流量单位及文件指纹；空值、零、负数、NaN、无穷大及非数值都会触发重新后处理。任意文件名缺少工况/版本不使物理量缓存失效，手工补齐工况但未改版本的记录也可以命中缓存。
- 提取脚本将工况从扫描结果名固化为显式列。任意命名的 `.res` 可提取物理量，但会警告无法推断工况，需补齐显式列。
- 重新提取（`-Force`、文件指纹变化、总压比无效、扫描时重新后处理）只刷新物理量：新记录的 `Speed_RPM` / `Back_Pressure_Pa` 为空时沿用旧行的非空值，手工补填的工况不会丢失。
- `extract_existing_map_data.ps1` 保留备份并重建表的行为，复用 `data_only.ps1 -Force`，统一提取口径；备份表通过 `-MetadataCsv` 只提供无法从文件名推断的工况。

## 统一绘图入口

```bash
python -m pip install -r requirements.txt
python plot_compressor_map.py --input examples/compressor_map.csv --output out.png
python plot_compressor_map.py --input examples/compressor_map.csv --output total.png --pr-type total --design-point examples/design_point.csv
python -m unittest discover -s tests -v
pwsh -NoProfile -File tests/test_map_data.ps1
```

Python GUI 测试需要解释器提供 tkinter/Tcl（不打开桌面窗口），验证设置保存和实际绘图命令。最后一个检查额外需要 PowerShell 7 和 Perl；用本地 CEL 数值替身执行生成的 Perl 宏，验证写表、缓存和换算，不需要 CFX。

GUI 的“求解与绘图参数 → 绘图压比”可选择“静压比”或“静止系总压比”；默认静压比，选择会保存到设置文件，并用于手动绘图和扫描完成后的自动绘图。旧结果缺少 `Total_PR` 时需要重新提取。

`--pr-type static|total` 默认 `static`，保持原 CLI 和 GUI 的静压比语义。优化目标对接时显式使用 `--pr-type total`。坐标轴随所选口径变化。`--design-point` 接受同格式的一行 CSV，使用同一压比选项及流量缩放规则，以星号标注；不参与等转速线修剪和喘振拟合。

`--efficiency-input` 沿用可选效率表；需要同一压比口径以进行插值。效率表缺少所选压比列（如旧表没有 `Total_PR`）或版本不受支持时，警告并忽略效率表，主图照常输出。设置 `--efficiency-input ""` 可禁用。`--blade-count` 默认 10。现有曲线修剪/喘振拟合属于绘图启发式，未在本次改动中重新标定。

## Windows + CFX 验证步骤

1. 备份真实结果目录和 CSV，确认 `cfdpost`、`cfx5solve` 可用，边界名与上述 CEL 一致。
2. 在已有 `.res` 目录执行 `pwsh -File <仓库>/data_only.ps1 -BladeCount 10 -MassFlowUnit kg/s -Force`（若 CFD-Post 原始流量显示 g/s，则使用 `g/s`）。检查新增版本、工况列及 `Total_PR`。
3. 在 CFD-Post 手工求值上述四个压力平均值，将两个比值与 CSV 对照（总压比写入 6 位小数）；核对单流道出口流量 × 叶片数后的 kg/s。
4. 无 `-Force` 再运行，未改动的 `.res` 应命中缓存。删掉 CSV 的 `Total_PR` 后再运行，应重新提取、每个结果仅保留一行。
5. 在可运行的扫描案例执行现有 `cfx.ps1` 扫描命令，确认新点写入相同列，复用旧结果时补提取总压比；旧 CSV 有备份且原行保留。
6. 分别以 `--pr-type static`、`total` 出图，检查轴标题、总压比与手工值；用设计点 CSV 核对星号位置。GUI 中切换“绘图压比”，确认手动及自动绘图传入相应 `--pr-type`，重启后保留选择。

本地离线检查不验证 CFX 许可证、真实网格/边界或求解收敛；以上真实结果检查由 Windows 环境执行。
