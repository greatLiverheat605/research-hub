---
name: literature-report-ppt
description: >-
  End-to-end academic literature reading, beginner-friendly deep report writing,
  figure-by-figure and formula-by-formula explanation, and Journal Club PPT/PPTX
  generation (powered by codex-ppt). Use this skill whenever the user asks to
  read/summarize a scientific paper, write a literature report (文献报告/文献综述),
  explain paper figures and mathematical formulas in plain language, or create
  an academic Journal Club presentation (文献汇报 PPT / 组会汇报).
---

# 学术文献深度研读与组会汇报全流程技能 (Literature Report & Journal Club PPT Skill)

本技能将**深度学术文献研读**、**零基础友好的图表与公式拆解**以及**基于 `codex-ppt` 的组会汇报 PPT 生成**整合为一套标准化、可复用的全流程工作流。

---

## 核心工作流概览 (Workflow Overview)

当用户提供一篇学术论文（PDF、DOI、PMC ID、arXiv 链接或本地文本）并要求进行文献研读或制作文献汇报时，按以下四个阶段推进：

### 阶段一：论文全文与图表素材获取 (Source & Figure Extraction)
1. **优先读取本地论文或通过 DOI/PMC 获取全文**：
   - 若用户提供了 DOI 或 PMC ID，可调用内置脚本 [`scripts/fetch_parse_pmc.py`](./scripts/fetch_parse_pmc.py) 自动从 NCBI PMC 下载全文 XML、解析结构化章节（Abstract / Introduction / Methods / Results / Discussion / References）、提取全部数学公式（LaTeX）并下载高清论文原图至工作区 `paper_figures/` 目录。
   - **铁律**：所有分析必须 100% 基于论文原文与真实引用文献，严禁脱离原文主观臆造数据或结论。

### 阶段二：撰写结构化深度文献报告 (Structured Deep Literature Report)
阅读并遵循 [`references/report-structure-guide.md`](./references/report-structure-guide.md) 中的规范，输出严格兼容 **Notion / Notero Markdown** 的七大模块深度报告：
1. **摘要 (Abstract)**：高信息密度概述研究主题、动机、核心问题、方法、主要发现与结论。
2. **背景 (Background)**：阐明问题背景、学术语境、研究空白与研究意义，并在结尾明确列出成对匹配的 **研究问题 (Research Questions, RQs)** 与 **研究假设 (Hypotheses, Hs)**。
3. **文献综述 (Literature Review)**：采用“主题分类为主、时间演进为辅”的混合分类策略，归纳核心引用文献的观点、方法与贡献，对每篇文献进行**批判性分析（指出局限性）**，梳理从“已有研究不足”到“本文研究问题”的逻辑演进，并补充领域内未被引用但重要的相关文献进行对比。
4. **方法 (Methods)**：清晰拆解研究对象/生物体系（Participants/Systems）、数据集与材料（Materials/Datasets）、数学建模与算法流程（Procedure/Pipeline）。
5. **实验和结果 (Experiments & Results)**：按实验模块详述实验设置、操作流程、量化指标与关键结果，并逐一呼应验证前文提出的研究假设（H1–Hn）。
6. **讨论 (Discussion)**：提炼核心发现，剖析作者的深入讨论维度（如物理/数学机制、参数解耦、预测价值），并系统对比本研究与既有文献的异同及原因。
7. **结论 (Conclusion)**：严格依据作者原文概括研究的**局限性与不足 (Limitations)** 以及**未来发展方向 (Future Directions)**。

### 阶段三：通俗化总结与图表、公式全景精讲 (Plain-Language Summary, Formulas & Figures Walkthrough)
面向跨学科读者或领域初学者，阅读并遵循 [`references/formula-figure-explainer-guide.md`](./references/formula-figure-explainer-guide.md)，提供：
1. **“三句话 + 核心比喻”通俗总结**：用生活化类比（如弹簧网络、社交距离、油水相分离等）解释核心算法与科学发现。
2. **全公式逐符号精讲 (Equation-by-Equation Walkthrough)**：列出论文全部核心公式，按 **【公式形式】→【符号字典】→【大白话直觉】→【为什么这么设计】** 四步法逐一拆解。
3. **全图表逐子图导读 (Figure-by-Figure & Panel-by-Panel Walkthrough)**：按 `Fig. 1` 至最后一幅图的顺序，逐个子图（`a, b, c, d...`）讲解 **【展示了什么/坐标轴含义】→【核心观察结果】→【证明了什么结论】→【组会汇报金句】**。

### 阶段四：文献汇报（Journal Club）PPT 规划与生成 (PPT Generation via `codex-ppt`)
当用户需要生成文献汇报 PPT 或规划组会演示文稿时：
1. 阅读 [`references/journal-club-ppt-structure.md`](./references/journal-club-ppt-structure.md) 了解学术组会汇报的 **12–16 页黄金结构**。
2. 联动已安装的 [`codex-ppt`](../codex-ppt/SKILL.md) 技能：
   - 先基于黄金结构生成 `outline.md`（明确每页角色、3–5 条核心要点，并将 `paper_figures/FigX.png` 作为 `Required images` 严格绑定到对应实验页）。
   - 推荐使用 [`references/academic-journal-club-style.md`](./references/academic-journal-club-style.md)（学术文献组会风）、`codex-ppt` 内置的 `科研答辩风`、`教学课件风` 或 `手绘技术解释风`。
   - 严格遵循 `codex-ppt` 的确认门控（确认大纲 → 确认视觉风格 → 确认生图后端 → 生成 1 页样张确认 → 批量生成全套幻灯片 → 撰写逐页口语化讲稿 `speech.md` → 调用 `assemble_ppt.py` 组装 `.pptx`）。

---

## 写作与格式强制规范 (Formatting Rules)

1. **中英术语对照**：所有专有名词或论文核心概念首次出现及重点总结时，必须使用**加粗**并在括号内注明英文全称及缩写，例如 **距离矩阵到结构系综 (Distance Matrix to Ensemble of Structures, DIMES)**。
2. **段首引导句**：每个自然段的第一句话必须加粗或明确概括该段中心思想，做到“只读段首句即可把握全文骨架”。
3. **统一文献引用格式**：正文引用统一采用 `（作者, 年份）` 格式，如 `（Bintu et al., 2018；Su et al., 2020）`。
4. **Notion Markdown 严格兼容**：
   - 使用标准一级至三级标题（`#`, `##`, `###`）；
   - 行内数学公式使用单美元符号 `$...$`，独立公式块使用双美元符号 `$$...$$` 且前后换行；
   - 避免在 Markdown 表格或正文中混用破坏 Notion 解析的复杂内联 HTML 标签。

---

## 参考文档与脚本索引 (Reference Map)

- [`scripts/fetch_parse_pmc.py`](./scripts/fetch_parse_pmc.py)：根据 DOI 或 PMC ID 自动抓取论文 XML、提取章节/公式/参考文献并下载高清原图。
- [`references/report-structure-guide.md`](./references/report-structure-guide.md)：七大模块深度文献报告写作指南与检查清单。
- [`references/formula-figure-explainer-guide.md`](./references/formula-figure-explainer-guide.md)：初学者友好的数学公式四步拆解法与科学图表五维导读法。
- [`references/journal-club-ppt-structure.md`](./references/journal-club-ppt-structure.md)：学术组会（Journal Club）12–16 页 PPT 黄金结构、时间分配与讲稿模板。
- [`references/academic-journal-club-style.md`](./references/academic-journal-club-style.md)：专为文献汇报 PPT 定制的视觉风格定义（可直接放入 `codex-ppt` 风格库复用）。
