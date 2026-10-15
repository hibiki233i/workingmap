# workingmap：压气机特性图工具

workingmap 用来给一个压气机设计画出**特性图**（也叫工作图）：横轴是流量，纵轴是压比和效率，
每条曲线对应一个转速，还会标出喘振边界和设计点。它调用 ANSYS CFX 在不同转速和出口背压下逐点计算，
再把结果整理成表格并出图。程序带一个 Windows 下的中文图形界面，用来配置扫描、监控进度、一键出图。

## 在整条链路中的位置

```
叶轮优化 / 叶片优化 ──→ 选定一个推荐设计 ──→ 性能评估（本仓库）──→ 特性图
```

- 前两个仓库（[叶轮优化](https://github.com/hibiki233i/cryogenic-compressor-impller-optimization)、
  [叶片优化](https://github.com/hibiki233i/compressor_blade_optimizition)）给出推荐设计。
- 本仓库把选定的设计变成一次完整的转速扫描，画出特性图，并在图上标出该设计所在位置。
- 衔接这一步的是 `from_optimization.py`，用法见 [优化结果 → 特性图](docs/from_optimization.md)。

## 两种使用方式，先分清

| | 离线快速开始 | 真实 CFD 运行 |
|---|---|---|
| 适用系统 | macOS / Linux / Windows 都行 | **只能在 Windows 上**，且装有 ANSYS CFX |
| 需要 CFX 吗 | 不需要 | 需要 |
| 数据 | 仓库自带的合成数据 | 你自己的 CFD 结果 |
| 能做什么 | 画图、转换优化结果、运行测试 | 真正扫描转速、得到特性图 |

自带示例的数据都是合成的，**不是测量值，也不是 CFD 结果**。

## 环境要求

### Python

- Python 3.11 或更新（自动测试用 3.11；作者在 macOS 上验证过 3.14）。
- 依赖写在 `requirements.txt`：numpy 和 matplotlib。
- 图形界面还要求 Python 带有 tkinter（Windows 的官方安装包默认带；部分 Linux 需要另外安装 `python3-tk`）。
  只出图、不开界面则不需要。

### 外部软件（真实 CFD 运行才需要）

| 软件 | 已验证的版本 | 说明 |
|---|---|---|
| ANSYS CFX | 2025 R1（v251） | 需要能调用 `cfx5solve`、`cfdpost` |
| PowerShell | 7 | 运行扫描和提取脚本 |
| Windows | — | 以上流程的运行平台 |

如果系统找不到 CFX 命令，可以在图形界面里填写 CFX 的 `bin` 目录，程序只在本次扫描中临时加入搜索路径。

### 需要自备的模板（未随仓库提供）

- 求解输入文件（`.def`）
- 基础 CCL 文件（`.ccl`），其中必须包含两个扫描会改写的表达式：转速 `MySpeed` 和出口背压 `MyBackPressure`
- 可选：作为初始流场的 `.res` 文件

## 离线快速开始

```sh
python3 -m venv .venv
source .venv/bin/activate          # Windows 用 .venv\Scripts\activate
python -m pip install -r requirements.txt
python plot_compressor_map.py --input examples/compressor_map.csv --output out.png
python -m unittest discover -s tests -v
```

第三行安装依赖；第四行用自带的合成数据画一张特性图 `out.png`；第五行运行全部自动测试。

再试几个常用变体：

```sh
# 用总压比画图，并标出设计点
python plot_compressor_map.py --input examples/compressor_map.csv --output total.png \
    --pr-type total --design-point examples/design_point.csv

# 一条命令演示"优化结果 → 特性图"整条链路（叶轮和叶片两条，合成数据）
python examples/from_optimization/run_example.py
```

另有一项需要 PowerShell 7 和 Perl 的测试，可检查结果写入、缓存和单位换算（不需要 CFX）：

```sh
pwsh -NoProfile -File tests/test_map_data.ps1
```

## 真实 CFD 运行（Windows）

1. 安装 Python、PowerShell 7 和 ANSYS CFX，并准备好自备的模板。
2. 双击 `start_cfx_gui.bat` 打开界面（它会自动寻找 Python）。
3. 在界面中选择 `.def` 和基础 `.ccl`，填写核数、叶片数、流量单位，编辑扫描点表格，点击开始。
4. 扫描完成后自动生成特性图（可在设置里关闭）。

完整的界面说明见 [图形界面使用指南](docs/gui_guide.md)。从优化结果开始的完整流程见
[优化结果 → 特性图](docs/from_optimization.md)。

## 仓库里有什么

| 文件 / 目录 | 作用 |
|---|---|
| `cfx_gui.py`、`start_cfx_gui.bat` | 中文图形界面与一键启动脚本 |
| `cfx.ps1` | 主扫描脚本：读取扫描点，调用 CFX 求解与后处理，把结果写入表格 |
| `data_only.ps1` | 对目录下已有的 `.res` 文件批量提取结果，不重新求解 |
| `extract_existing_map_data.ps1` | 重建旧结果表，提取口径与上面保持一致 |
| `map_data.ps1` | 以上脚本共用的表格格式定义 |
| `plot_compressor_map.py` | 画特性图 |
| `from_optimization.py` | 把优化结果中的一个设计转换成扫描输入和设计点 |
| `scan_points.csv` | 默认的扫描点配置 |
| `examples/` | 合成示例数据，以及"优化结果 → 特性图"的完整演示 |
| `docs/` | 详细文档（见下） |
| `tests/` | 自动测试 |

## 文档索引

- [图形界面使用指南](docs/gui_guide.md)：界面怎么用，扫描过程怎么运作
- [数据格式说明](docs/data_format.md)：扫描点表和结果表每一列的含义、单位换算、旧表迁移
- [优化结果 → 特性图](docs/from_optimization.md)：如何由优化结果自动生成扫描输入和出图命令，以及 Windows 上的验证步骤
- [演示示例说明](examples/from_optimization/README.md)：自带示例包含什么

## 测试

- `python -m unittest discover -s tests -v`：全部 Python 测试，不需要 CFX，也不需要桌面显示。
  GUI 测试需要 tkinter，只检查设置保存和绘图命令，不会打开窗口。
- 数值回归测试 `tests/test_numerical_regressions.py` 用入库的合成特性图，核对喘振边界拟合、
  边界偏移和效率插值的数值，防止修改后结果悄悄改变。
- GitHub Actions 在 Ubuntu + Python 3.11 上运行同样的测试。
- 以上测试**不验证** CFX 许可证、真实网格、边界设置或求解收敛，这些需要在 Windows 工程机上手动验证。

## 注意事项

- 仓库不再带 `base.def` 和 `base.ccl`，需自行提供。
- 图形界面的设置保存在仓库目录下的 `.cfx_gui_settings.json`，已被版本控制忽略。
- 压比有"静压比"和"总压比"两种口径。默认画静压比；与优化目标对照时要用总压比（`--pr-type total`）。
- 曲线修剪和喘振线拟合属于经验性做法，没有重新标定。
