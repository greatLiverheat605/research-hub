---
name: source-grounded-method-audit
description: Use when auditing research methods, numerical claims, and reproduction evidence across papers and code.
---

# Source-Grounded Method Audit（源码与实证对齐审计）

本技能用于核查论文正文、实验总结与原始代码/JSON 输出之间是否一致，杜绝“论文写一个数、代码算另一个数”的脱节与伪完成。

## 核心审计原则

1. **逐数溯源**：论文或周报中出现的每一个样本分母 `n`、区间界限、效应量、种子数与耗时，必须能在源码或真实落盘的 `.json` / `.log` 中找到唯一对应出处。
2. **禁止脑补合并**：未找到运行产物或字段缺失时，必须明确标记为 `缺失/未复现`，绝不凭印象填补或沿用旧版本结果。
3. **跨硬件时间禁比**：不同机器（Mac CPU vs Colab T4 vs Windows GPU）测得的 wall-clock 秒数不可直接横向比较。
4. **按需查阅细则**：完整分场景审计协议（CSV 回写、增量纠偏、全文本仲裁、字段修复等）全部保存在本目录的 [references/audit-protocols.md](references/audit-protocols.md) 中；**仅当遇到对应细分审计场景时按需检索读取其中对应小节**，禁止一次性通读全文浪费上下文额度。
