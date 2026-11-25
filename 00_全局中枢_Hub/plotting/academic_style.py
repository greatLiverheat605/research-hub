"""
00_全局中枢_Hub/plotting/academic_style.py
全局统一顶刊绘图样式包（Nature / Science 子刊规范）。

特性：
- Okabe-Ito 色盲友好调色板 + 海军蓝/青绿项目主题色板
- 顶刊单栏 (88 mm) / 1.5栏 (127 mm) / 双栏 (180 mm) 标准画布宽度
- 自动隐藏上/右坐标轴脊、统一字号层级、外向刻度、中文 Arial/Microsoft YaHei 回退
- 一键导出高分辨率 PNG (450 DPI) + 矢量 PDF/SVG
"""

from pathlib import Path
from typing import Iterable, List, Tuple

PALETTE_OKABE_ITO = [
    "#0072B2",  # Blue
    "#D55E00",  # Vermilion
    "#009E73",  # Bluish green
    "#CC79A7",  # Reddish purple
    "#E69F00",  # Orange
    "#56B4E9",  # Sky blue
    "#F0E442",  # Yellow
    "#24313E",  # Dark slate
]

PALETTE_NAVY_TEAL = {
    "navy": "#0B1C2C",
    "teal": "#529286",
    "teal_bg": "#E7F0ED",
    "ink": "#24313E",
    "warn": "#C46A2B",
    "warn_bg": "#FBF0E6",
    "muted": "#64748B",
}

FIG_WIDTH_SINGLE_IN = 3.46   # 88 mm (单栏)
FIG_WIDTH_15COL_IN = 5.00    # 127 mm (1.5 栏)
FIG_WIDTH_DOUBLE_IN = 7.09   # 180 mm (双栏)


def apply_nature_style(base_font_size: float = 8.0, use_cjk_fallback: bool = True) -> None:
    """Apply publication-grade Matplotlib rcParams globally."""
    import matplotlib as mpl
    from cycler import cycler

    sans_fonts = ["Arial", "Helvetica", "DejaVu Sans"]
    if use_cjk_fallback:
        sans_fonts = ["Arial", "Helvetica", "Microsoft YaHei", "PingFang SC", "SimHei", "DejaVu Sans"]

    mpl.rcParams.update({
        "font.family": "sans-serif",
        "font.sans-serif": sans_fonts,
        "font.size": base_font_size,
        "axes.titlesize": base_font_size + 1.0,
        "axes.titleweight": "bold",
        "axes.labelsize": base_font_size,
        "xtick.labelsize": base_font_size - 1.0,
        "ytick.labelsize": base_font_size - 1.0,
        "legend.fontsize": base_font_size - 1.0,
        "legend.frameon": False,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.linewidth": 0.75,
        "axes.edgecolor": PALETTE_NAVY_TEAL["ink"],
        "axes.labelcolor": PALETTE_NAVY_TEAL["ink"],
        "axes.prop_cycle": cycler(color=PALETTE_OKABE_ITO),
        "xtick.direction": "out",
        "ytick.direction": "out",
        "xtick.major.width": 0.75,
        "ytick.major.width": 0.75,
        "lines.linewidth": 1.4,
        "lines.markersize": 4.5,
        "axes.unicode_minus": False,
        "pdf.fonttype": 42,   # TrueType fonts embedded cleanly for Illustrator
        "ps.fonttype": 42,
        "svg.fonttype": "none",
    })


def style_axes(ax, grid_axis: str = "") -> None:
    """Clean spines and optionally add light dashed gridlines."""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    if grid_axis in ("x", "y", "both"):
        ax.grid(axis=grid_axis, linestyle="--", linewidth=0.5, alpha=0.35, color="#94A3B8")
        ax.set_axisbelow(True)


def add_panel_label(ax, label: str, x: float = -0.14, y: float = 1.08, fontsize: float = 10.0) -> None:
    """Add bold lowercase panel identifier (a, b, c) outside upper-left corner."""
    ax.text(
        x,
        y,
        label,
        transform=ax.transAxes,
        fontsize=fontsize,
        fontweight="bold",
        va="top",
        ha="left",
        color=PALETTE_NAVY_TEAL["navy"],
    )


def place_outside_legend(ax, ncol: int = 1, loc: str = "upper left", bbox_to_anchor: Tuple[float, float] = (1.02, 1.0)):
    """Place legend cleanly outside plot area to prevent overlapping data curves (orx-figures standard)."""
    return ax.legend(loc=loc, bbox_to_anchor=bbox_to_anchor, ncol=ncol, frameon=False, borderaxespad=0.0)


def audit_figure_layout(fig) -> List[str]:
    """Run automated publication QA checks (width, min font size, legend-data overlap)."""
    warnings: List[str] = []
    try:
        w_in, h_in = fig.get_size_inches()
        if w_in < 2.8 or w_in > 7.6:
            warnings.append(f"画布宽度 {w_in:.2f}in 偏离期刊标准栏宽（建议单栏 3.46in / 1.5栏 5.00in / 双栏 7.09in）")
        fig.canvas.draw()
        renderer = fig.canvas.get_renderer()
        for idx, ax in enumerate(fig.axes):
            for txt in ax.texts + [ax.title, ax.xaxis.label, ax.yaxis.label]:
                if txt.get_text().strip() and txt.get_fontsize() < 6.0:
                    warnings.append(f"子图 #{idx+1} 存在过小字号 ({txt.get_fontsize():.1f}pt < 6pt): '{txt.get_text()[:20]}'")
            leg = ax.get_legend()
            if leg is not None and leg.get_visible():
                leg_box = leg.get_window_extent(renderer)
                for line in ax.get_lines():
                    xy = line.get_xydata()
                    if len(xy) == 0:
                        continue
                    pts = ax.transData.transform(xy)
                    if any(leg_box.contains(px, py) for px, py in pts):
                        warnings.append(f"子图 #{idx+1} 图例遮挡了数据点，建议改用 place_outside_legend(ax)")
                        break
    except Exception:
        pass
    return warnings


def save_pub_fig(
    fig,
    output_stem: str,
    dpi: int = 450,
    formats: Iterable[str] = ("pdf", "png"),
    tight: bool = True,
    audit: bool = True,
) -> List[Path]:
    """Save a figure simultaneously to vector (PDF/SVG) and high-DPI raster (PNG) with optional layout QA."""
    if audit:
        for w in audit_figure_layout(fig):
            print(f"⚠️ [academic_style QA] {w}")
    stem_path = Path(output_stem)
    stem_path.parent.mkdir(parents=True, exist_ok=True)
    saved: List[Path] = []
    kwargs = {"bbox_inches": "tight", "pad_inches": 0.04} if tight else {}
    for fmt in formats:
        out_path = stem_path.with_suffix(f".{fmt.lstrip('.')}")
        fig.savefig(out_path, dpi=dpi, **kwargs)
        saved.append(out_path)
    return saved
