---
name: peer-review-rebuttal
description: Use when drafting point-by-point responses to journal peer reviewers, organizing major/minor revision plans, or deciding which comments require new experiments versus evidence-based rebuttal.
---

# Peer Review Rebuttal（顶刊审稿意见逐条回复与修稿规范）

本技能用于在收到期刊/会议审稿意见后，协助 PI 制定不卑不亢、证据闭环的修稿计划与 Point-by-Point Response Letter，同时严守课题 `_control/00_CONTRACT.md` 与 `02_DECISION_LEDGER.md` 边界。

## 1. 审稿意见分级拆解矩阵（先分类，再动笔）

将每位 Reviewer 的每条意见拆成独立编号（如 `R1.1`、`R1.2`、`R2.1`），并归入以下 4 类之一：
1. **A 类（事实补强 / 局部澄清）**：正文表述不清、缺少图表标注或置信区间说明 -> 直接改稿并引用新行号。
2. **B 类（必须补做对照/消融实验）**：直接击中方法学短板或关键基线缺失 -> 登记任务进 `01_PROJECT_STATE.md`，补实验后再写回执。
3. **C 类（超出本文范围 / 方法学误解）**：审稿人按另一任务口径要求不合理改动 -> **绝不盲目妥协**；先肯定其问题视角，再用已有消融数据或数学边界礼貌定界，必要时在正文 Discussion/Scope 补充一句话澄清。
4. **D 类（重大分歧 / 需仲裁）**：可能动摇核心假设 -> 浓缩入 `_bus/ask.md` 发起 `consult all` 或切 `Opus` 仲裁后再答复。

## 2. 单条回复标准四段结构（Point-by-Point Template）

每条回复必须包含：
1. **Reviewer 原文摘录**（逐字保留，不删改）。
2. **核心结论先行**：1 句话明确说明“已修改 / 已补充实验 X / 说明适用边界”。
3. **实证依据与分析**：给出具体数值、统计区间、物理/数学解释，**严禁空洞致谢或防御性道歉**（避免 `We apologize for...` / `We are limited by...`）。
4. **正文修改定位（Action Taken）**：明确写出改在 `Section X, Page Y, Line Z` 或 `Figure/Table N`，并附上**高亮后的修改文本块**。

## 3. 提交前自检门禁

- 是否每一条审稿意见都有对应编号且无遗漏？
- 回复信中的所有新数值是否与正文、附图及最新 JSON 日志 100% 一致（可配合 `source-grounded-method-audit` 复核）？
- 新增或删改的决定是否已追加写入 `_control/02_DECISION_LEDGER.md`？
