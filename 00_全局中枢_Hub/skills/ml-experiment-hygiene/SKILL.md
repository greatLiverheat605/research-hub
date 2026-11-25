---
name: ml-experiment-hygiene
description: Use before or while writing ML, diffusion, sampling, or bioinformatics experiment scripts (local or Colab) to enforce split isolation, anti-leakage assertions, deterministic seeding, checkpoint resume, and COMPLETE_OK structured outputs.
---

# ML Experiment Hygiene（计算实验防泄漏、断点续跑与自通知规范）

本技能用于在编写本地 Python 或 Google Colab 实验脚本**之前与之中**，从工程源头杜绝信息泄漏、随机性不可复现、Colab 断线白跑以及结果口径混乱。

## 1. 五条硬性工程铁律

1. **划分隔离与防泄漏断言（Zero Leakage）**：
   - 在特征工程、归一化尺度估计、正则选参及采样启动前，必须显式写入断言检查 `Train ∩ Cal == ∅`、`Train ∩ Test == ∅`、`Cal ∩ Test == ∅`；
   - 若课题契约（`_control/00_CONTRACT.md`）规定了测试集封存门禁，测试集路径必须默认锁定或显式加 `--allow-test` 开关，禁止在开发期默认读取。
2. **确定性种子与环境指纹**：
   - 统一设置 `random`、`numpy`、`torch`（含 `cuda.manual_seed_all`）随机种子，并将 `seed`、核心超参、Git/脚本版本、硬件设备（CPU/GPU 型号）写入结果 JSON 的 `metadata` 字段。
3. **Colab / 长耗时实验断点续跑（Checkpoint & Resume）**：
   - 凡预计运行超过 5 分钟的实验或网格扫描，必须按单元/样本定期增量写入中间结果（JSONL 或分片 `.pt`/`.npz`），启动时自动跳过已完成样本，防止 Colab 断连从头重跑。
4. **统一结构化输出 Schema**：
   - 每次实验结束必须写出单份汇总 JSON（包含 `complete: true/false`、样本分母 `n`、核心指标点估计、置信区间与耗时），禁止只打印在终端滚动日志里。
5. **完成自通知标记（配合 `runnotify` / `watchdone`）**：
   - 脚本正常结束时在末尾打印 `COMPLETE_OK` 并把最终结果文件原子重命名落盘（先写 `.tmp` 再 `os.replace`），确保宿主机 `watchdone` 监听到完整无截断的结果文件后弹窗提醒 PI。

## 2. 跨机公平性禁区

- 不同硬件环境（如 Mac CPU vs Colab T4 vs Windows GPU）测得的绝对运行时间（wall-clock seconds）**严禁直接横向合并对比**；做速度基准必须在同一机器、相同预热与并发条件下重测。
