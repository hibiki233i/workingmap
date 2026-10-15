# 数据格式说明

本文说明 workingmap 用到的两张表：**扫描点表**（告诉程序要算哪些工况）和**特性图数据表**（算出来的结果）。
它们是"优化结果 → 性能评估 → 特性图"这条链路中的统一约定，当前版本号为 **1**（`schema_version = 1`）。

> 文档和示例里的数据都是**合成的**，仅用于离线验证，不代表测量值或 CFD 结果。

## 一、扫描点表（scan_points.csv）

告诉程序每条转速线要从哪个背压开始算。一行对应一条等转速线。UTF-8 编码的 CSV（可带 BOM），逗号分隔，小数点用 `.`。

| 列名 | 含义 | 单位 |
|---|---|---|
| `SpeedRPM` | 转速，正整数 | rpm |
| `InitialPressurePa` | 这条转速线的起始出口背压 | Pa |

示例（`examples/scan_points.csv`）：

```csv
SpeedRPM,InitialPressurePa
8000,5
8500,5
9000,5
```

背压是写入 CFX 边界条件的**设定值**，沿用模型自己的参考压力约定，不是实测的出口绝对压力。
当前扫描脚本对起始背压设了 16 Pa 的安全上限，具体到你的模型，要检查边界和参考压力是否一致。

## 二、特性图数据表

文件名可以任意（常见的是 `Compressor_Map_Data.csv` 或 `Extracted_Compressor_Data.csv`），列的顺序不限。
新写入的完整记录版本号为 1。**空值表示"不知道"，不能用 0 代替缺失的数据。**

画图至少需要：质量流量、所选的压比、转速和背压。其余物理量按来源提供即可。

| 列名 | 含义 | 单位 / 取值 |
|---|---|---|
| `schema_version` | 数据格式版本。空表示旧格式或没有补齐的旧记录 | `1` |
| `Result_File` | 来源标识，通常是 `.res` 文件名。工况列齐全时可省略 | — |
| `Speed_RPM` | 工况转速，优先于从文件名读取的值 | rpm |
| `Back_Pressure_Pa` | 工况出口背压设定值，优先于从文件名读取的值 | Pa |
| `Mass_Flow_kg_s` | 出口质量流量，**始终是 kg/s**，已乘以流道倍数 | kg/s |
| `Blade_Count` | 这行流量代表的流道倍数：1 表示单流道，10 表示单流道 × 10 | 正数 |
| `Mass_Flow_Unit` | CFD-Post 原本输出的流量单位，只是提取时的记录，**不是**上一列的单位 | `kg/s` 或 `g/s` |
| `Static_PR` | 静压比：出口与进口质量流量平均静压之比 | 无量纲 |
| `Total_PR` | 总压比：出口与进口质量流量平均总压之比（静止坐标系） | 无量纲 |
| `P_in_Pa`、`P_out_Pa` | 进口、出口平均静压 | Pa |
| `T_in_K`、`T_out_K` | 进口、出口平均静温 | K |
| `Isentropic_Efficiency` | 出口平均等熵压缩效率，通常在 0 到 1 之间；为兼容旧表，大于 1 的值按百分数处理 | — |
| `Result_LastWriteUtcTicks` | 可选：`.res` 的修改时间，用于判断结果是否过期 | — |
| `Result_Size_Bytes` | 可选：`.res` 文件大小，用于判断结果是否过期 | byte |

### 两种压比怎么理解

- **静压比**：出口平均静压 ÷ 进口平均静压。
- **总压比**：出口平均总压 ÷ 进口平均总压。这里的总压采用**静止坐标系**，不用旋转坐标系，
  与叶轮优化仓库 `cfx_post/Extract_Results.cse` 的提取口径一致。

两个平均值先各自算好再相除。总压比**不会**用静压比代替。边界必须与 `.res` 中的名称 `R1 Inlet`、`R1 Outlet` 一致。
如果进口平均压力为零，提取时写 0，画图时会跳过这样的无效点并报告文件和行号。

### 流量的换算

提取时：整机流量（kg/s）= CFD-Post 读到的出口流量 × 单位换算 × `Blade_Count`。
其中 g/s 的换算因子是 0.001，kg/s 是 1。

画图时：图上的流量（g/s）= `Mass_Flow_kg_s` × 绘图时指定的叶片数 ÷ `Blade_Count` × 1000。

例如表里记录 0.002 kg/s、`Blade_Count` 为 10：用 `--blade-count 10` 画图得到 2 g/s，用 `--blade-count 20` 则是 4 g/s。
即使 `Mass_Flow_Unit` 是 g/s，也不会在画图时再做一次单位换算。

## 三、旧表怎么办

旧版本生成的表缺少新增的列，程序会尽量自动兼容，同时给出提醒：

- 缺少 `Speed_RPM` 或 `Back_Pressure_Pa` 时，逐项从文件名 `Map_Speed_<转速>_Press_<背压>` 中补读，并发出警告。
  明确写了但不合法的数值不会被文件名覆盖。
- 缺少 `Blade_Count` 时警告，并按单流道处理。如果流量单位无法确认，请先核实表里的数值确实是 kg/s。
- 流量、压比、工况或叶片数无效的行会被跳过，并报告行号；没有任何有效行时命令失败。版本号未知则直接报错。
- 画图时选 `--pr-type total` 要求表里有 `Total_PR`，**不会**退回去用静压比。旧数据需要先重新提取。
- 扫描脚本 `cfx.ps1` 读到列头与标准不一致的旧表时，会先备份为 `.legacy_upgrade_<时间>.bak`，再保留旧的行、
  从文件名补齐能识别的工况，之后只保留标准列。你自己加的列（如备注）只会留在备份里。
- 批量提取脚本 `data_only.ps1` 同样会重新提取总压比缺失或无效的旧缓存，并覆盖同名记录。
  找不到对应 `.res` 的旧行保留原样，未知项留空，不会被标成版本 1。
- 判断"缓存是否可以复用"时，要求总压比是有限的正数，并核对叶片数、原始流量单位和文件指纹。
  空值、零、负数、NaN、无穷大或非数字，都会触发重新提取，但不会重新运行求解器。
- 重新提取只会刷新物理量；新记录里转速或背压为空时，沿用旧行里的非空值，手工补填的工况不会丢。
- 任意文件名的 `.res` 也可以提取物理量，但无法推断工况，会发出警告，需要手工补齐转速和背压列。
- `extract_existing_map_data.ps1` 会先备份再重建表，内部复用 `data_only.ps1 -Force`，保证提取口径统一。
  备份表通过 `-MetadataCsv` 只提供那些无法从文件名推断的工况。

## 四、画图命令

```sh
python -m pip install -r requirements.txt
python plot_compressor_map.py --input examples/compressor_map.csv --output out.png
python plot_compressor_map.py --input examples/compressor_map.csv --output total.png \
    --pr-type total --design-point examples/design_point.csv
python -m unittest discover -s tests -v
pwsh -NoProfile -File tests/test_map_data.ps1
```

| 选项 | 作用 |
|---|---|
| `--pr-type static`（默认）/ `total` | 选择画静压比还是总压比，坐标轴标题随之变化。与优化目标对照时用 `total` |
| `--design-point <文件>` | 一行 CSV（格式同上），用星号标出设计点，旁边的说明框列出效率、压比、流量和工况。设计点不参与曲线修剪与喘振拟合 |
| `--design-label <文字>` | 说明设计点来源 |
| `--style map`（默认）/ `paper` | `map` 是按效率着色的特性图；`paper` 是论文风格：(a) 压比—流量、(b) 效率—流量，衬线字体、向内刻度、空心标记，设计点在两张子图都标出；`.png` 为 600 dpi，`.pdf`、`.svg` 是矢量图 |
| `--efficiency-input <文件>` | 可选的效率表，必须与所选压比口径一致才能插值；缺少所选压比列或版本不支持时，警告并忽略，主图照常输出。填 `""` 可关闭 |
| `--blade-count` | 绘图使用的叶片数，默认 10 |

曲线的修剪和喘振线拟合属于经验做法，没有重新标定。

最后两条命令的测试条件：图形界面测试需要 Python 带 tkinter（不会打开窗口）；PowerShell 的测试还需要 PowerShell 7 和 Perl，
它用本地模拟值运行脚本，验证写表、缓存和换算，不需要 CFX。

## 五、Windows + CFX 上的验证步骤

离线检查不能验证 CFX 许可证、真实网格、边界设置和求解收敛。在 Windows 工程机上，建议按下面的顺序核对：

1. 备份真实的结果目录和结果表，确认 `cfdpost`、`cfx5solve` 可用，边界名与上文一致。
2. 在已有 `.res` 的目录运行 `pwsh -File <仓库>/data_only.ps1 -BladeCount 10 -MassFlowUnit kg/s -Force`
   （如果 CFD-Post 里原始流量显示为 g/s，就写 `g/s`）。检查新增的版本号、工况列和 `Total_PR`。
3. 在 CFD-Post 里手工求出进出口两个压力的平均值，把静压比、总压比与表里的值对照（总压比写入 6 位小数）；
   核对"单流道出口流量 × 叶片数"是否等于表里的 kg/s。
4. 不加 `-Force` 再运行一次，没改动的 `.res` 应当直接命中缓存。把表里的 `Total_PR` 删掉再运行，应当重新提取，
   而且每个结果只保留一行。
5. 在一个能跑通的扫描案例上运行 `cfx.ps1`，确认新点写入同样的列，复用旧结果时会补提取总压比，旧表有备份且原行保留。
6. 分别用 `--pr-type static` 和 `total` 出图，核对坐标轴标题和总压比；用设计点 CSV 核对星号位置。
   在图形界面里切换"绘图压比"，确认手动和自动绘图都传入了对应选项，并且重启后选择保留。
