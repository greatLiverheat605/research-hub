---
name: academic-ppt-studio
description: 开源学术汇报与周报 PPT 全流程制作技能（集成 python-pptx 海军蓝/青绿学术设计系统、Marp Markdown 转 PPTX 流水线、以及自动化排版重叠与禁用词质检器）。适用于任意课题的组会与答辩汇报（Track B）。
---

# Academic PPT Studio（学术周报与组会 PPT 制作技能）

本技能专为下游汇报交付（Track B）设计，在**绝不污染或反向改写科研主线（Track A）结论**的前提下，在当前课题的 `04_组会与周报_Weekly/<YYYY-MM-DD>_汇报主题/` 目录下快速产出达到研究生组会/学术答辩水准的 `.pptx` 演示文稿与配套逐字讲稿。写入前须遵守当前课题 `04_组会与周报_Weekly/README_周报写入规范.md`。

---

## 一、支持的两套开源制作引擎（按需选择）

### 引擎 A：`python-pptx` 海军蓝/青绿学术设计系统（推荐用于正式周报与组会，精确控制像素级卡片、原生表格与矢量图）
沿用 16:9 宽屏（`13.333" × 7.5"`）学术设计规范：
- **配色规范**：
  - 主色海军蓝 `NAVY = RGBColor(0x0B, 0x1C, 0x2C)`（顶栏、主标题、重点边框）
  - 强调青绿 `TEAL = RGBColor(0x52, 0x92, 0x86)` / 浅底 `TEAL_BG = RGBColor(0xE7, 0xF0, 0xED)`
  - 正文深灰黑 `INK = RGBColor(0x24, 0x31, 0x3E)` / 卡片浅底 `CARD = RGBColor(0xF3, 0xF7, 0xF5)`
  - 风险/边界警示橙 `WARN = RGBColor(0xC4, 0x6A, 0x2B)` / 浅底 `WARN_BG = RGBColor(0xFB, 0xF0, 0xE6)`
- **字体规范**：全局使用 `Microsoft YaHei`（同时写入 `a:ea` 与 `a:cs` XML 节点，防止中文回退为宋体）。

### 引擎 B：`Marp CLI` / `Pandoc` 极速 Markdown 转 PPTX（适合 5 分钟快速草稿）
若只需快速将 Markdown 大纲转为可编辑 PPTX，可使用：
```bash
npx -y @marp-team/marp-cli@latest slides.md --pptx -o 周报-快速版.pptx
```

---

## 二、严格学术表述纪律（对齐当前课题 `_control/00_CONTRACT.md`）

在生成 PPT 标题、卡片正文与 `02-逐页演讲稿.md` 时，必须先读取当前课题的 `_control/00_CONTRACT.md`「不宣称清单」，**严禁出现夸张或违背方法学边界的词汇**：
- ❌ **通用禁用词黑名单**：`颠覆认知`、`真冠军`、`喂错数据`、`纹丝不动`、`一图看懂`、`绝对标准答案`、`准确率`、`正确率`、`首次用成像比较`、`首个统一基准`、`恢复了真实三维结构`。
- ✅ **规范学术表述**：使用具体量化指标、95% 置信区间（注明单/双侧与分母）、数据划分边界与真实适用范围进行客观表述。

---

## 三、四步交付与自动化质检流程

每次在当前课题 `04_组会与周报_Weekly/<YYYY-MM-DD>_汇报主题/` 下执行周报任务时，必须按顺序完成：

1. **Step 1：只读采集主线素材**
   - 读取当前课题的 `_control/00_CONTRACT.md`、`_control/01_PROJECT_STATE.md`、`_control/02_DECISION_LEDGER.md` 及最新实测结果文件，写出 `01-周报大纲.md`。（可配合 `consult glm_heavy` 生成大纲初稿与脚本）
2. **Step 2：编写逐页口语化讲稿**
   - 写出 `02-逐页演讲稿.md`（每页含：核心结论一句话、数据来源路径、口语化讲稿、听众可能提问的边界回答）。
3. **Step 3：编写并运行 `build_pptx.py` 生成 `.pptx`**
4. **Step 4：运行自动化质检脚本（必过门禁）**
   ```bash
   python3 "$RESEARCH_HUB_ROOT/00_全局中枢_Hub/skills/academic-ppt-studio/scripts/check_pptx_quality.py" \
     "<当前课题>/04_组会与周报_Weekly/<YYYY-MM-DD>_汇报主题/周报-<YYYY-MM-DD>.pptx"
   ```
   质检脚本会自动检查：页数、禁用词黑名单、文本框越界（超出 `13.333" × 7.5"` 画布）以及空文本占位符。只有输出 `PASS` 才算交付完成。
