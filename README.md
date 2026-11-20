# ResearchHub · 多 AI 协同科研总控中枢

> 一个面向个人/课题组的轻量级多 AI 协同科研工作流框架：**单一主控 + 只读红队/科学顾问 + 隔离课题舱 + 科研总控大屏 + 论文/实验工程护栏**。零第三方 Python 重依赖，兼容 macOS/Linux 原生 Python 3.9+。

---

## 核心特性

- **物理隔离的课题舱（`new_project.py`）**
  - 一键在 `01_在研课题_Projects/` 创建标准化课题目录（`_control/`、`_bus/`、`01_论文与协议/`、`02_实验与验证/`、`03_代码与脚本/`、`04_组会与周报_Weekly/`），自动登记 `WORKSPACE_INDEX.md` 并生成 `p01`/`p02` 秒切别名。
  - **SSOT 三件套**：`00_CONTRACT.md`（科研红线与不宣称清单）、`01_PROJECT_STATE.md`（作战沙盘）、`02_DECISION_LEDGER.md`（只增不删决策台账）。
- **跨模型只读会诊总线（`consult.py` + `models.toml`）**
  - 通过 `_bus/ask.md` / `_bus/reply.md` 调度外部模型进行红队挑刺（`redteam`）、科学顾问裁决（`advisor`）、双盲并发（`all`）或周报/PPT 辅助（`glm_heavy` / `glm_fast`），自动归档到 `_bus/archive/`。
  - 支持 `--model` 临时切模型、`--effort` 调推理深度、`--eli5` 通俗解释、`--render` 调用 `glow` 终端 Markdown 排版、`--dry-run` 0 消耗预览。
- **工程与学术防错护栏（`scripts/`）**
  - `mcp_filesystem_server.py`：带保护目录与只增台账校验的本地 MCP 文件服务。
  - `check_manuscript.py`（`checkms`）：扫描断链、待核引文、禁用夸大词与防御性表述。
  - `codex_sandbox.py`（`codexbox`）：将子模块编码权限锁定在 `03_代码与脚本/<子模块>/`。
  - `notify_when_done.py`（`runnotify` / `watchdone`）：0 消耗监控本地/Colab 长实验并自动留存 `run_manifest_*.json`。
  - `ppt_studio.py`（`ppt`）：周报/组会可编辑 PPTX 生成与排版质检。
- **科研总控大屏（`dashboard/serve.py`）**
  - 本地 `http://127.0.0.1:8765/` 可视化驾驶舱，集成健康体检、参数化场景指令台（含 Colab CLI 模板）、模型甜点位与文件树预览。
- **按需懒加载科研 Skill 库（`00_全局中枢_Hub/skills/`）**
  - 涵盖立项评估（`idea-evaluator`）、方法与数值审计（`source-grounded-method-audit`、`verification`）、统计分析、论文精修、引文核验、绘图与顶刊投稿检查。

---

## 目录结构

```text
research-hub/
├── 00_全局中枢_Hub/
│   ├── _project_template/     # 新课题标准模板（CLAUDE.md / _control / _bus / 01~04）
│   ├── dashboard/             # 本地科研总控大屏 (serve.py + index.html)
│   ├── plotting/              # 科研绘图统一风格模板
│   ├── scripts/               # checkms / codexbox / runnotify / ppt_studio / mcp_filesystem_server
│   ├── skills/                # 按需懒加载科研 Skill 专区
│   ├── consult.py             # 多模型会诊 CLI
│   ├── models.toml            # 模型路由与系统提示词热插拔配置
│   ├── models.example.toml    # 模型配置示例模板
│   ├── new_project.py         # 一键建舱脚本 (newproj)
│   ├── policy.toml            # 只读归档与只增台账保护策略
│   ├── OPERATION_MANUAL.md    # 详细操作手册
│   └── WORKSPACE_INDEX.md     # 在研课题总览表
├── .env.example               # API Key 环境变量模板
├── install.sh                 # 一键初始化目录与 shell 别名
└── LICENSE
```

---

## 快速开始

```bash
git clone https://github.com/greatLiverheat605/research-hub.git
cd research-hub
chmod +x install.sh
./install.sh
```

1. 按提示将输出的别名块加入 `~/.zshrc` 并 `source ~/.zshrc`。
2. 编辑 `~/.config/research_hub/.env`，填入你需要的 API Key（`DEEPSEEK_API_KEY` / `OPENAI_API_KEY` / `GLM_API_KEY`），并按需调整 `00_全局中枢_Hub/models.toml`。
3. 创建你的第一个课题并启动大屏：

```bash
newproj "P01_My_Research" --goal "一句话描述核心科学问题" --activate
consult all --dry-run
dash
```

> 可选：安装 `brew install glow` 后，可在终端用 `consult all --render` 或 `mdview` 直接排版阅读 `_bus/reply.md`。
