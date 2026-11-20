---
name: latex-rhythm-refiner
description: >
  Post-process LaTeX project prose to improve readability through varied
  sentence and paragraph lengths. Removes filler phrases and unnecessary
  transitions while preserving all citations and semantic meaning. Use
  after drafting is complete, when sections read as monotonous or blocky.
---

> **全局课题通用守则（Hub Standard Override）**
> 1. 规则优先级：当前课题 `_control/02_DECISION_LEDGER.md` > `_control/00_CONTRACT.md` > `CLAUDE.md` > 本 Skill。
> 2. 严禁编造文献、数字或实验结论；所有定量指标与区间必须源自本课题真实运行结果或已核验文献。
> 3. 方法局限与边界限定语集中写在 Discussion / 局限节，正文不逐句防御叠甲。
> 4. 统计指标遵循顶刊规范：报告效应量与 95% 置信区间（注明单/双侧、重采样单位与合格分母 $n$）。
> 5. 本技能仅用于英文 LaTeX 稿件节奏润色；中文稿件请使用 `writing-core`。


# LaTeX Rhythm Refiner

## When to Use
- After LaTeX content generation is complete
- To improve prose flow and readability in academic documents
- When sections feel monotonous or blocky

## When NOT to Use
- During initial content drafting
- For citation verification or addition
- For technical/structural LaTeX fixes

## Core Principles

### 1. Preserve Citations Exactly
- Every `\cite{...}` must remain in place
- Citations stay attached to their original semantic context
- Never move a citation to a different claim or sentence meaning

### 2. Vary Rhythm Stochastically
**Sentence length**: Mix short (5-12 words), medium (13-22 words), and long (23-35 words) sentences. Avoid consecutive sentences of similar length.

**Paragraph length**: Alternate between:
- Short (2-3 sentences) for emphasis or transitions
- Medium (4-5 sentences) for standard exposition
- Long (6-8 sentences) for complex arguments

### 3. Remove Fillers
Eliminate or replace:
- "in order to" → "to"
- "it is worth noting that" → (delete or rephrase directly)
- "due to the fact that" → "because"
- "in the context of" → "in" or "for"
- "a large number of" → "many"
- "in spite of the fact that" → "although"
- "at the present time" → "now" or "currently"
- "for the purpose of" → "to" or "for"

### 4. Minimize Transitions
Remove when structure already implies the relationship:
- "However," / "Therefore," / "Moreover," / "Furthermore,"
- "As mentioned above," / "As previously discussed,"
- "It should be noted that" / "In this regard,"

### 5. Tighten Prose
- Prefer active voice; replace vague verbs ("shows", "does", "works") with concrete ones
- Replace mid-sentence em-dashes with commas, colons, or a new sentence
- Use semicolons sparingly (at most one per paragraph as a rule of thumb)
- Avoid repeated sentence openings across adjacent sentences ("This", "In practice", etc.)
- Replace hedge stacks ("may potentially") with one qualifier
- Each paragraph: one main idea, clear first sentence
- Ensure figures/tables are referenced and explained in text

## Processing Workflow

### Per-Section Refinement
Process one section at a time:

1. **Read** the section fully to understand context and argument flow
2. **Identify** all `\cite{...}` locations and their attached claims
3. **Map** current sentence/paragraph lengths
4. **Refine**:
   - Vary sentence lengths (break long chains, combine choppy sequences)
   - Adjust paragraph boundaries for rhythm
   - Strip fillers and unnecessary transitions
5. **Verify** all citations remain with their original semantic claims
6. **Output** the refined section

### Verification Checklist
Before finalizing each section:
- [ ] Citation count unchanged
- [ ] Each citation still supports its original claim
- [ ] No 3+ consecutive sentences of similar length
- [ ] Paragraph lengths vary
- [ ] Filler phrases removed
- [ ] Unnecessary transitions eliminated
- [ ] Technical accuracy preserved

## Constraints
- **Do not** add, remove, or relocate citations
- **Do not** change technical claims or data
- **Do not** alter LaTeX commands, environments, or structure
- **Do not** modify figure/table references or captions
- **Do not** expand abbreviations or change terminology
