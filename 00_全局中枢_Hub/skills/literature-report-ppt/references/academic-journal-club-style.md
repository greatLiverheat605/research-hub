# 学术文献组会风

**适用场景:**
- 课题组文献汇报（Journal Club / 文献精读组会）
- 生物信息学、计算生物学、人工智能与物理建模论文分享
- 研究生课程文献研读展示与学术沙龙
- 需要将复杂数学公式、算法流程图与论文原始实验大图（Multi-panel Figures）清晰结合的学术演示

**GPT-Image-2 风格 Brief:**
```json
{
  "type": "16:9 full-slide PowerPoint image",
  "style_name": "学术文献组会风",
  "best_for": "课题组文献汇报（Journal Club）、论文精读分享、计算生物学与AI算法图解、实验大图拆解与公式推导讲解",
  "visual_direction": "modern Nature/Cell journal club presentation style, crisp scientific clarity, beginner-friendly visual hierarchy, action-title driven layout, seamless integration of multi-panel paper figures, mathematical formula callout boxes, and plain-language takeaway strips",
  "canvas": {
    "aspect_ratio": "16:9",
    "background": "clean off-white #FAFBFD with pure white #FFFFFF content cards and subtle #E2E8F0 borders",
    "composition": "top action-title banner, main split-screen or 3-column body (left: original paper figure or formula derivation; right: structured bullet breakdown & physical intuition cards), bottom one-sentence Take-home Message bar",
    "density": "medium-high information density with generous padding inside cards so complex equations and figure annotations breathe clearly"
  },
  "color_palette": {
    "primary": "Nature navy blue #1B365D and slate blue #2B547E",
    "secondary": "bio-teal #0D9488 and soft sky tint #F0F9FF for formula/intuition highlight boxes",
    "accent": "crimson coral #DC2626 or warm amber #D97706 for key quantitative metrics (e.g., r > 0.99, JSD = 0.02) and critical biological conclusions",
    "neutral": "ink black #0F172A, body charcoal #334155, muted caption gray #64748B",
    "rule": "use navy and teal for structural hierarchy and mathematical intuition cards; reserve crimson/amber strictly for key quantitative findings and biological takeaways"
  },
  "typography": {
    "title": "bold Chinese sans-serif action title at top-left, stating the main conclusion of the slide in one clear sentence",
    "section_labels": "pill-shaped or rounded-rectangle badges in navy or bio-teal with white bold text (e.g., 核心公式, 实验验证, 物理机制, 组会要点)",
    "body": "clean, highly legible Chinese sans-serif with bold lead-in phrases and clear English terms in parentheses",
    "math_and_metrics": "clean serif/math styling for equations with colored callout labels explaining key symbols",
    "text_quality": "all Chinese characters, English technical terms, and mathematical symbols must be sharp, accurate, and non-garbled"
  },
  "page_system": {
    "header": "top section tag (e.g., 01 背景与痛点 / 02 核心算法 / 03 实验结果) + large conclusion-driven Action Title + journal citation badge at top-right (e.g., Nature Commun. 2023)",
    "body": "flexible academic grid: left-figure-right-analysis for experimental slides; 3-step horizontal flow for methodology slides; 2x2 or 3-column comparison cards for literature review and discussion",
    "footer": "full-width subtle navy/teal takeaway bar at the bottom starting with '核心结论 (Take-home Message):'"
  },
  "layout_patterns": [
    "Journal Club Cover: paper title, journal badge, author/affiliation, visual graphical abstract card, and 3 core contributions preview",
    "Background & Gap Split: left side comparing Sequencing (Hi-C) vs Imaging (FISH); right side listing the 3 computational bottlenecks & research questions",
    "Formula & Intuition Board: center/left equation box with symbol dictionary arrows; right side physical analogy diagram (e.g., bead-spring network) and 3-step algorithm pipeline",
    "Paper Figure Deep-Dive (Left-Figure Right-Breakdown): strict preservation of supplied paper figure on the left (60% width), paired with panel-by-panel (a/b/c/d) structured annotations and quantitative badges on the right (40% width)",
    "Discussion & Critical Evaluation: pros/cons balance grid, limitations from original text, and future applications in disease/cancer genomics"
  ],
  "rendering_constraints": [
    "When a required paper figure is supplied, preserve its internal charts, heatmaps, scatter plots, axes, and labels faithfully without cropping or distorting data.",
    "Never clutter a slide with wall-of-text paragraphs; use structured cards, numbered badges, and clear visual arrows.",
    "No watermarks, no fictional university logos, no unrelated cartoon clip-art."
  ]
}
```
