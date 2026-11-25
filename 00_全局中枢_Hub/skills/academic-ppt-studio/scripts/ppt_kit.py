#!/usr/bin/env python3
"""
Academic PPT Studio 公共排版组件库 (ppt_kit.py)
供各课题 build_pptx.py 直接导入，大幅缩减脚本长度并统一海军蓝/青绿学术视觉规范。
"""

from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from PIL import Image
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.util import Inches, Pt

# 默认学术配色（海军蓝 + 青绿 + 暖金/告警）
NAVY = RGBColor(0x0B, 0x1C, 0x2C)
NAVY_MID = RGBColor(0x16, 0x33, 0x4D)
TEAL = RGBColor(0x0F, 0x76, 0x6E)
TEAL_LIGHT = RGBColor(0xE6, 0xF4, 0xF1)
INK = RGBColor(0x1E, 0x29, 0x3B)
MUTED = RGBColor(0x64, 0x74, 0x8B)
BG = RGBColor(0xF8, 0xFA, 0xFC)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
BORDER = RGBColor(0xCB, 0xD5, 0xE1)
WARN_BG = RGBColor(0xFE, 0xF3, 0xC7)
WARN_FG = RGBColor(0xB4, 0x53, 0x09)
OK_BG = RGBColor(0xDC, 0xFC, 0xE7)
OK_FG = RGBColor(0x15, 0x80, 0x3D)

FONT_ZH = "PingFang SC"
FONT_EN = "Arial"


def new_prs() -> Presentation:
    """Create 13.333 x 7.5 in widescreen presentation."""
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    return prs


def blank_slide(prs: Presentation, bg_color: RGBColor = BG):
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = bg_color
    return slide


def set_run_font(run, size: float, color: RGBColor = INK, bold: bool = False, font_zh: str = FONT_ZH, font_en: str = FONT_EN) -> None:
    run.font.name = font_en
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color
    rPr = run._r.get_or_add_rPr()
    for tag in ("a:ea", "a:cs"):
        el = rPr.find(qn(tag))
        if el is None:
            el = rPr.makeelement(qn(tag), {})
            rPr.append(el)
        el.set("typeface", font_zh)


def add_rect(slide, x: float, y: float, w: float, h: float, fill_color: RGBColor, line_color: Optional[RGBColor] = None, line_pt: float = 0.75, rounded: bool = False):
    shape_type = MSO_SHAPE.ROUNDED_RECTANGLE if rounded else MSO_SHAPE.RECTANGLE
    sh = slide.shapes.add_shape(shape_type, Inches(x), Inches(y), Inches(w), Inches(h))
    sh.fill.solid()
    sh.fill.fore_color.rgb = fill_color
    if line_color is not None:
        sh.line.color.rgb = line_color
        sh.line.width = Pt(line_pt)
    else:
        sh.line.fill.background()
    return sh


def add_text(
    slide,
    x: float,
    y: float,
    w: float,
    h: float,
    text: str,
    size: float = 13,
    color: RGBColor = INK,
    bold: bool = False,
    align=PP_ALIGN.LEFT,
    valign=MSO_ANCHOR.TOP,
):
    tb = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = tb.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = valign
    tf.margin_left = Inches(0.05)
    tf.margin_right = Inches(0.05)
    tf.margin_top = Inches(0.03)
    tf.margin_bottom = Inches(0.03)
    lines = str(text).split("\n")
    for idx, line in enumerate(lines):
        p = tf.paragraphs[0] if idx == 0 else tf.add_paragraph()
        p.alignment = align
        p.space_after = Pt(2)
        run = p.add_run()
        run.text = line
        set_run_font(run, size=size, color=color, bold=bold)
    return tb


def add_header(slide, title: str, tag: str = "", subtitle: str = "") -> None:
    add_rect(slide, 0, 0, 13.333, 0.88, NAVY)
    add_rect(slide, 0, 0.88, 13.333, 0.04, TEAL)
    if tag:
        add_text(slide, 0.4, 0.10, 4.0, 0.24, tag, size=10, color=TEAL_LIGHT, bold=True)
        add_text(slide, 0.4, 0.32, 12.2, 0.46, title, size=20, color=WHITE, bold=True)
    else:
        add_text(slide, 0.4, 0.18, 12.2, 0.52, title, size=21, color=WHITE, bold=True)
    if subtitle:
        add_text(slide, 0.4, 0.96, 12.5, 0.28, subtitle, size=11, color=MUTED)


def add_footer(slide, page_no: int, total_pages: int, source_text: str = "") -> None:
    add_rect(slide, 0.4, 7.08, 12.53, 0.015, BORDER)
    if source_text:
        add_text(slide, 0.4, 7.12, 10.8, 0.28, source_text, size=9.5, color=MUTED)
    add_text(slide, 11.4, 7.12, 1.53, 0.28, f"{page_no} / {total_pages}", size=9.5, color=MUTED, align=PP_ALIGN.RIGHT)


def add_card(slide, x: float, y: float, w: float, h: float, title: str = "", accent: RGBColor = TEAL):
    add_rect(slide, x, y, w, h, WHITE, line_color=BORDER, line_pt=0.75, rounded=True)
    add_rect(slide, x, y, w, 0.05, accent)
    if title:
        add_text(slide, x + 0.14, y + 0.10, w - 0.28, 0.32, title, size=13.5, color=NAVY, bold=True)


def add_table(slide, x: float, y: float, w: float, h: float, headers: Sequence[str], rows: Sequence[Sequence[str]], col_widths: Optional[Sequence[float]] = None, font_size: float = 10.5):
    nrows = len(rows) + 1
    ncols = len(headers)
    shape = slide.shapes.add_table(nrows, ncols, Inches(x), Inches(y), Inches(w), Inches(h))
    tbl = shape.table
    if col_widths:
        for idx, cw in enumerate(col_widths):
            tbl.columns[idx].width = Inches(cw)
    for c, hdr in enumerate(headers):
        cell = tbl.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = NAVY
        cell.vertical_anchor = MSO_ANCHOR.MIDDLE
        p = cell.text_frame.paragraphs[0]
        p.alignment = PP_ALIGN.LEFT
        r = p.add_run()
        r.text = str(hdr)
        set_run_font(r, size=font_size, color=WHITE, bold=True)
    for r_idx, row in enumerate(rows, start=1):
        bg = WHITE if r_idx % 2 == 1 else BG
        for c_idx, val in enumerate(row):
            cell = tbl.cell(r_idx, c_idx)
            cell.fill.solid()
            cell.fill.fore_color.rgb = bg
            cell.vertical_anchor = MSO_ANCHOR.MIDDLE
            p = cell.text_frame.paragraphs[0]
            p.alignment = PP_ALIGN.LEFT
            r = p.add_run()
            r.text = str(val)
            set_run_font(r, size=font_size, color=INK, bold=False)
    return shape


def add_picture_fit(slide, img_path: Path, x: float, y: float, max_w: float, max_h: float):
    """Insert image preserving aspect ratio inside (x, y, max_w, max_h)."""
    p = Path(img_path)
    if not p.is_file():
        add_rect(slide, x, y, max_w, max_h, BG, line_color=BORDER)
        add_text(slide, x + 0.1, y + max_h * 0.42, max_w - 0.2, 0.4, f"图缺失: {p.name}", size=11, color=MUTED, align=PP_ALIGN.CENTER)
        return None
    with Image.open(p) as im:
        iw, ih = im.size
    ratio = min(max_w / iw, max_h / ih)
    dw, dh = iw * ratio, ih * ratio
    px = x + (max_w - dw) / 2
    py = y + (max_h - dh) / 2
    return slide.shapes.add_picture(str(p), Inches(px), Inches(py), width=Inches(dw), height=Inches(dh))


def add_notes(slide, notes_text: str) -> None:
    if notes_text:
        slide.notes_slide.notes_text_frame.text = str(notes_text)
