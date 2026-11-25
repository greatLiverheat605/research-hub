---
name: submission-package-check
description: Use when preparing a manuscript for journal submission (Nature Communications, NeurIPS, Bioinformatics, etc.) to verify Cover Letter, Abstract limits, Data/Code Availability statements, Reporting Summary items, and cross-artifact consistency.
---

# Submission Package Check（顶刊投稿前打包与合规核查）

本技能用于论文初稿进入投稿冲刺阶段（如 *Nature Communications* 或计算生物学顶刊）时，逐项核查除正文外的全套投稿材料与形式合规性。

## 1. 核心投稿材料逐项清单

1. **Title & Abstract**：
   - 核对目标期刊字数上限（如 NC 摘要通常 ≤150 词、无未定义缩写、无参考文献引用）。
   - 检查是否遵守课题 `_control/00_CONTRACT.md` 的不宣称清单（禁用“首个”“绝对标准”“颠覆”等越界表述）。
2. **Cover Letter（编辑部信）**：
   - 3～4 段精炼结构：核心科学问题与空白 → 本工作的具体量化突破与验证范围 → 为何契合该刊读者群 → 利益冲突/未一稿多投/伦理与数据公开声明。
   - 必须给出 3～5 位合理的同行审稿人推荐方向与回避说明（如有）。
3. **Data & Code Availability（数据与代码可用性声明）**：
   - 正文声明、GitHub/Zenodo 占位或发布快照、以及公共数据集编号（GEO/ENCODE/4DN 等访问号）是否完全一致。
   - 禁止在未公开前填写无效死链，待补链接必须标注为明确待填项并在最终提交前清零。
4. **Reporting Summary / 统计学报告要求**：
   - 样本量 `n`、划分单位（染色体/位点/细胞/区块）、误差棒定义（SD/SEM/Bootstrap CI 与单双侧）、随机种子与软件版本号是否全部交代清楚。
5. **图表与附表对齐**：
   - 主图（Main Figures）、补充图（Supplementary Figures）与正文首次引用顺序是否严格递增且无悬空引用。

## 2. 配套收口检查

- 先在宿主机运行 `checkms --project "<课题>"` 确保断链、待核占位符与禁用词清零；
- 最终提交前必须配合 `verification` 技能完成一轮实证门禁签字。
