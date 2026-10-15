# 离线端到端示例：优化结果 → 特性图

```bash
python examples/from_optimization/run_example.py [--output-dir DIR]
```

脚本对叶轮和叶片各运行一次 [`from_optimization.py`](../../docs/from_optimization.md)，然后用解析式特性图代替 CFX 扫点，最后用转换器生成的出图命令画图：

| 输出（默认 `example-output/from_optimization/`） | 内容 |
| --- | --- |
| `impeller_runs/Runs/Run_XXX/` | 每个训练样本一个替身运行目录：`input_parameters.json`、`CFX_Results.txt`（单流道）、占位 `.out` / `.res`；与真实 runner 一样没有 `Impeller.def` |
| `blade_runs/cases/case_XXXXXX/` | `candidate.json`、`cfx_state.json`（`p_out_pa=12`、`n_blades=9`）、`CFX_Results.txt`、占位 `Impeller.def` / `.res` |
| `impeller/`、`blade/` | 转换器输出（`scan_points.csv`、`base_map.ccl`、`design_point.csv`、`manifest.json`）、合成的 `Compressor_Map_Data.csv` 和 `compressor_map_total.png` |
| `impeller_def.ccl`、`blade_def.ccl` | 替身 CCL，分别演示“转速写死，需要 `--bind-speed-domain R1`”和“已由 `MySpeed` 驱动”两种情况，不代表真实模板 |

选点与预期结果：

| 链路 | 选择 | 定位结果 | 效率 | 总压比 | 流量 g/s | 流道数 |
| --- | --- | --- | --- | --- | --- | --- |
| 叶轮 | `engineering_rank=1` | 在 384 个目录中按设计向量匹配到 `Run_031` | 0.74867 | 1.68194 | 4.49925 | 11 |
| 叶片 | `run_id=case_000004` | 记录的 `D:\...` 路径不存在，回退到 `blade_runs/cases/case_000004` | 0.76285 | 2.1 | 4.25017 | 9 |

合成特性图的设计转速线在设计背压处正好经过设计点，所以星号必须落在 10000 rpm 线上；测试会检查这一点。

## 数据来源

| 文件 | 来源 |
| --- | --- |
| `impeller/Compressor_Training_Data.csv` | [叶轮仓库](https://github.com/hibiki233i/cryogenic-compressor-impller-optimization) `6699081`，`python -m impeller_app.reproduce --config examples/offline/config.json`；与该仓库 `examples/offline/Compressor_Training_Data.csv` 逐字节相同 |
| `impeller/pareto_engineering_ranked.csv` | 同一次运行的 `pareto_engineering_ranked.csv`，未改动 |
| `blade/training_data.csv`、`blade/pareto_front.csv` | [叶片仓库](https://github.com/hibiki233i/compressor_blade_optimizition) `ae42ea7`，`python benchmarks/run_offline_baseline.py`（与参考值比对通过）；`case_dir` 列原为生成机器上的绝对路径，已替换为 `D:\blade_runs\offline_baseline\cases\<run_id>`，以模拟换机器后的情况 |

所有数值都是**合成数据**，不是 CFD 结果。也可以用 `--impeller-dir`、`--blade-dir` 指向两个仓库新运行的输出目录。
