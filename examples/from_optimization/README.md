# 演示示例：优化结果 → 特性图

```sh
python examples/from_optimization/run_example.py [--output-dir DIR]
```

脚本对叶轮和叶片各运行一次 [`from_optimization.py`](../../docs/from_optimization.md)，
再用一条解析公式代替 CFX 扫描，生成一张"假想的"特性图，最后用转换器给出的出图命令画图。
全程不需要 CFX。**所有数据都是合成的，不是 CFD 结果。**

## 输出了什么

默认写在 `example-output/from_optimization/`：

| 位置 | 内容 |
|---|---|
| `impeller_runs/Runs/Run_XXX/` | 每个训练样本一个替身运行目录：输入参数、单流道结果、占位的 `.out` 和 `.res`。与真实的叶轮程序一样，没有 `Impeller.def` |
| `blade_runs/cases/case_XXXXXX/` | 叶片的替身算例：`candidate.json`、`cfx_state.json`（背压 12 Pa、9 个叶片）、结果文件和占位的 `.def`、`.res` |
| `impeller/`、`blade/` | 转换器的输出（扫描点、基础 CCL、设计点、manifest），合成的特性图数据，以及论文风格的图 `compressor_map_static.png`：(a) 静压比—流量，(b) 效率—流量 |
| `impeller_def.ccl`、`blade_def.ccl` | 替身的 CCL 文本，分别演示"转速写死，需要 `--bind-speed-domain R1`"和"转速已由 `MySpeed` 驱动"两种情形，不代表真实模板 |

## 选了哪个设计、预期结果

| 链路 | 选择 | 定位结果 | 效率 | 静压比 | 总压比 | 流量 g/s | 流道数 |
|---|---|---|---|---|---|---|---|
| 叶轮 | 工程排名第 1 | 在 384 个目录中按设计参数匹配到 `Run_031` | 0.74867 | 1.24463（取自结果文件） | 1.68194 | 4.49925 | 11 |
| 叶片 | `run_id=case_000004` | 记录里的 `D:\...` 路径不存在，回退到 `blade_runs/cases/case_000004` | 0.76285 | 2.0 | 2.1 | 4.25017 | 9 |

合成特性图中，设计转速线在设计背压处正好经过设计点，所以图上的星号必须落在 10000 rpm 那条线上，测试会检查这一点。
需要矢量图时，把 `manifest.json` 里出图命令的 `--output` 改成 `.pdf` 或 `.svg` 再运行。

## 数据来源

| 文件 | 来源 |
|---|---|
| `impeller/Compressor_Training_Data.csv` | [叶轮仓库](https://github.com/hibiki233i/cryogenic-compressor-impller-optimization) 提交 `6699081`，运行 `python -m impeller_app.reproduce --config examples/offline/config.json`；与该仓库的 `examples/offline/Compressor_Training_Data.csv` 逐字节相同 |
| `impeller/pareto_engineering_ranked.csv` | 同一次运行的 `pareto_engineering_ranked.csv`，未改动 |
| `blade/training_data.csv`、`blade/pareto_front.csv` | [叶片仓库](https://github.com/hibiki233i/compressor_blade_optimizition) 提交 `ae42ea7`，运行 `python benchmarks/run_offline_baseline.py`（与参考值比对通过）。`case_dir` 列原本是生成机器上的绝对路径，已替换为 `D:\blade_runs\offline_baseline\cases\<run_id>`，用来模拟换机器后的情况 |

也可以用 `--impeller-dir`、`--blade-dir` 指向两个仓库新运行出来的输出目录。
