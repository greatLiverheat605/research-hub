# 全局顶刊绘图样式包使用说明 (`academic_style.py`)

所有科研课题的 Python 画图脚本只需在开头加入两行即可统一论文图件风格（默认 Okabe-Ito 色盲友好色板、88/180 mm 顶刊宽度、去上右轴脊、Embedded TrueType 字体、同时导出矢量 PDF + 450 DPI PNG）：

```python
import sys
from pathlib import Path

# 宿主机与跨课题通用定位方式（按实际层级调整 parents 索引）
HUB_PLOTTING = Path("$RESEARCH_HUB_ROOT/00_全局中枢_Hub/plotting")
sys.path.insert(0, str(HUB_PLOTTING))

import matplotlib.pyplot as plt
from academic_style import (
    FIG_WIDTH_SINGLE_IN,
    FIG_WIDTH_DOUBLE_IN,
    PALETTE_OKABE_ITO,
    PALETTE_NAVY_TEAL,
    add_panel_label,
    apply_nature_style,
    save_pub_fig,
    style_axes,
)

apply_nature_style(base_font_size=8.0)

fig, ax = plt.subplots(figsize=(FIG_WIDTH_SINGLE_IN, 2.6))
# ... 你的绘图逻辑 ...
style_axes(ax, grid_axis="y")
add_panel_label(ax, "a")
save_pub_fig(fig, "figures/fig1_comparison", formats=("pdf", "png"))
```
