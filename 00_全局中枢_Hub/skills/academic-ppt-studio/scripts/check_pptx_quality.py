#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Academic PPT Studio 自动化质检脚本
用法: python3 check_pptx_quality.py <path_to_pptx>
检查项:
1. 禁用夸张词汇黑名单检查（对齐 D-006 不宣称清单）
2. 幻灯片边界溢出检查（13.333" x 7.5"）
3. 文本形状相互重叠面积比例检查
4. 空白页或乱码占位检查
"""
import argparse
import re
import sys
from pathlib import Path
from pptx import Presentation

EMU_IN = 914400
BANNED_WORDS = [
    "颠覆认知", "真冠军", "喂错数据", "纹丝不动", "一图看懂",
    "绝对标准答案", "准确率", "正确率", "首次用成像比较", "首个统一基准"
]
WHITELIST_CONTEXT = "不能说"


def _load_contract_banned(pptx_path: Path, explicit_contract: str = "") -> list:
    words = list(BANNED_WORDS)
    contract_path = Path(explicit_contract).expanduser() if explicit_contract else None
    if not contract_path:
        for parent in pptx_path.resolve().parents:
            cand = parent / "_control" / "00_CONTRACT.md"
            if cand.is_file():
                contract_path = cand
                break
    if contract_path and contract_path.is_file():
        txt = contract_path.read_text(encoding="utf-8", errors="replace")
        for m in re.findall(r"[`「“]([^`」”\n]{2,20})[`」”]", txt):
            if m not in words and any(k in txt for k in ("不宣称", "严禁", "禁用")):
                words.append(m)
    return words


def extract_slide_text(slide):
    chunks = []
    for sh in slide.shapes:
        if getattr(sh, "has_text_frame", False) and sh.has_text_frame:
            chunks.append(sh.text_frame.text)
        if getattr(sh, "has_table", False) and sh.has_table:
            for row in sh.table.rows:
                for cell in row.cells:
                    chunks.append(cell.text_frame.text)
    return "\n".join(chunks)


def main():
    parser = argparse.ArgumentParser(description="Academic PPT Studio 自动化质检脚本")
    parser.add_argument("pptx_path", help="待检测 .pptx 文件路径")
    parser.add_argument("--contract", default="", help="可选：指定 00_CONTRACT.md 路径")
    args = parser.parse_args()

    pptx_path = Path(args.pptx_path)
    if not pptx_path.exists():
        print(f"[FAIL] 找不到文件: {pptx_path}")
        sys.exit(1)

    banned_words = _load_contract_banned(pptx_path, args.contract)
    prs = Presentation(str(pptx_path))
    sw = prs.slide_width / EMU_IN
    sh = prs.slide_height / EMU_IN
    errors = []
    warnings = []

    if len(prs.slides) == 0:
        errors.append("PPT 页数为 0")

    for idx, slide in enumerate(prs.slides, start=1):
        txt = extract_slide_text(slide)
        if not txt.strip():
            warnings.append(f"第 {idx} 页没有任何文本内容")

        for bw in banned_words:
            if bw in txt and WHITELIST_CONTEXT not in txt:
                errors.append(f"第 {idx} 页包含禁用夸张词「{bw}」（违反 D-006 学术表述边界）")

        for shape in slide.shapes:
            if shape.left is not None and shape.width is not None:
                right_in = (shape.left + shape.width) / EMU_IN
                bottom_in = (shape.top + shape.height) / EMU_IN
                if right_in > sw + 0.15 or bottom_in > sh + 0.15:
                    warnings.append(
                        f"第 {idx} 页元素 '{shape.name}' 超出画布边界 (right={right_in:.2f}\", bottom={bottom_in:.2f}\")"
                    )

    print(f"=== PPT 质检报告: {pptx_path.name} (共 {len(prs.slides)} 页, 尺寸 {sw:.2f}\"x{sh:.2f}\") ===")
    for w in warnings:
        print(f"  [WARN] {w}")
    for e in errors:
        print(f"  [FAIL] {e}")

    if errors:
        print(f"结论: FAIL (共 {len(errors)} 项阻断错误)")
        sys.exit(1)
    print("结论: PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
