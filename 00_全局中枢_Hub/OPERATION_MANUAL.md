# 科研 AI 军团跨端协作操作手册（v1.0）

> **核心理念**：**一脑（Claude 唯一主控）+ 一手（宿主机/Colab 手动跑实验）+ 三外围（GPT 顾问 / DeepSeek 红队 / Gemini 文献 / GLM 交付）**。  
> 所有模型协同**只通过共享目录里的结构化文件交接**，彻底告别聊天窗口复制粘贴、额度偷跑与主权漂移。

---

## 1. 工作区总图（宿主机 ↔ 虚拟机共用）

| 分区 | 宿主机 Mac 路径 | 虚拟机内（`ClaudeShield`）路径 | 职责与访问纪律 |
|---|---|---|---|
| **全局中枢** | `.../文件交换/00_全局中枢_Hub/` | `.../ClaudeShield-Exchange/00_全局中枢_Hub/` | 模型热配置 `models.toml`、会诊点火器 `consult.py`、开新课题工具 `new_project.py`、本手册 |
| **在研课题舱** | `.../文件交换/01_在研课题_Projects/` | `.../ClaudeShield-Exchange/01_在研课题_Projects/` | 每个课题独立一个子目录（当前主课题：`P01_Demo_Project/`），彼此绝对隔离 |
| **公共资料库** | `.../文件交换/02_公共资料库_Library/` | `.../ClaudeShield-Exchange/02_公共资料库_Library/` | 跨课题共享 PDF 文献库与通用材料 |
| **历史归档区** | `.../文件交换/99_历史归档_Archive/` | `.../ClaudeShield-Exchange/99_历史归档_Archive/` | 冷备只读区（VM MCP 禁止写入；除非 PI 明确点名回查，否则禁止主动扫描） |

*(注：旧路径 `文件交换/Legacy_Project_Example` 已保留相对软链接指向 `01_在研课题_Projects/P01_Demo_Project`，既有工具进程不会断链。)*

---

## 2. 一次性环境配置（首次使用必看）

### 2.1 在虚拟机内：配置 Claude Desktop 本地 `filesystem` MCP（零 Node 依赖、保护只读区）
1. 因隔离虚拟机内通常未安装 Node.js / `npx`，请直接使用中枢提供的**纯 Python 3 本地 MCP 服务端**（`00_全局中枢_Hub/scripts/mcp_filesystem_server.py`，内置禁止写入 `00_全局中枢_Hub/`、`99_历史归档_Archive/`、`冻结件/` 与 `冻结存档-*` 的硬性保护）：
   - 在虚拟机内将 `mcp_filesystem_server.py` 复制到 `~/Library/Application Support/Claude/mcp_filesystem_server.py`（`chmod 755`）；
   - 在 `~/Library/Application Support/Claude/claude_desktop_config.json` 配置：
     ```json
     {
       "mcpServers": {
         "filesystem": {
           "command": "/usr/bin/python3",
           "args": [
             "-u",
             "/Users/<vm-user>/Library/Application Support/Claude/mcp_filesystem_server.py",
             "/Volumes/My Shared Files/ClaudeShield-Exchange"
           ]
         }
       }
     }
     ```
   *(配置完成后完全重启一次 Claude Desktop，对话框底部出现工具图标即成功。)*

3. **利用 Claude Desktop 的 `Projects` 功能彻底防串台**：
   - 在 Claude Desktop 左侧为每个课题单独新建 Project（如 `[P01] ...`、`[P02] ...`）；
   - 将下面这段通用模板粘贴进该 Project 的 **Project Instructions（项目指令）**（将 `<课题目录名>` 替换为实际目录）：
     ```text
     你当前负责的唯一课题路径是：
     /Volumes/My Shared Files/ClaudeShield-Exchange/01_在研课题_Projects/<课题目录名>

     每次开始对话时，先用 filesystem MCP 读取该目录下的：
     1) CLAUDE.md
     2) _control/00_CONTRACT.md
     3) _control/01_PROJECT_STATE.md
     4) _control/02_DECISION_LEDGER.md
     严格遵守其中红线与操作 SOP。禁止主动扫描 99_历史归档_Archive/ 或修改 冻结存档-20261117/。
     ```

### 2.2 在宿主机 Mac：配置 API 密钥与 `consult` 快捷命令
1. **填写宿主机本地密钥（虚拟机不可见）**：
   编辑 `~/.config/research_hub/.env`（权限已锁定为 `600`）：
   - `DEEPSEEK_API_KEY=` 填入你的 DeepSeek 官方 API Key（红队用）
   - `GLM_API_KEY=` 填入你的 GLM 渠道（`https://open.bigmodel.cn/api/paas/v4`）API Key（PPT/周报用）
   - `OPENAI_API_KEY=` **可留空**：`models.toml` 已配置 `use_cc_switch_active = true`，宿主机在运行时会自动从 `~/.cc-switch/cc-switch.db` 内存读取你当前激活的 Codex 渠道（目前为 `active-provider` / `gpt-6.2-astra`）！如果你希望固定用其他 Key，填入此处即可覆盖。
2. **宿主机终端（Ghostty / Pebrel 通用）与 `~/.zshrc` 快捷命令（已预置完成）**：
   - **Ghostty 配置文件**（`~/.config/ghostty/config`）：已配置 `Nord` 主题、`Menlo 13.5` 等宽字体、`50MB` 超大滚动历史、`copy-on-select = clipboard` 与 `macos-option-as-alt = true`；新开 Ghostty 窗口/标签页会自动显示 `[ResearchHub 科研中枢]` 导航提示。
   - **Pebrel 配置文件**（`~/Library/Application Support/Pebrel/pebrel.luau`）：保留原有的 `Local Mac AI CLI` 与 `Node B: Windows` 通道，并在 Profiles 顶部新增 **`[科研主线] P01 染色质块条件插补`**、**`[科研中枢] 00_全局中枢_Hub`** 与 **`[科研大屏]`** 三个直达启动项。
   - **`~/.zshrc` 常用短命令速查**（Ghostty 与 Pebrel 新开标签页自动生效，已开窗口执行一次 `source ~/.zshrc`）：
     - `p01` / `p02` / `hub`：秒切 P01 / P02 课题目录 / 全局中枢目录（`newproj` 自动生成后续 `p0X` 与 `p <关键词>`）
     - `consult all` / `consult redteam` / `consult advisor` / `consult glm_fast`：宿主机实时流式会诊（支持 `--effort`、`--no-stream` 与 `--model role=id`）
     - `ppt --topic "组会汇报"`：Track B 专用 GLM 多轮可编辑 PPT 引擎（仅写 `04_组会与周报_Weekly/`，自动过质检）
     - `runnotify` / `watchdone`：0 Token 本地或 Colab 实验完成弹窗自通知与 `run_manifest` 留痕
     - `codexbox <子目录>`：受限 Codex CLI 启动器（仅允许写 `03_代码与脚本/<子模块>/`）
     - `checkms`：一键论文断链、禁用词、去 AI 腔与防御句体检
     - `pview <文件.md>`：用 Pebrel 原生 GPU 渲染器阅读 Markdown（如 `pview _bus/reply.md`）
     - `newproj "P03_名称"`：一键创建独立新课题舱
     - `hubhelp` / `hubkeys` / `hubmodels` / `ghosttycfg`：CLI 速查 / 密钥 / 模型配置 / Ghostty 配置
     - `dash`：启动并打开本科研总控大屏（`http://127.0.0.1:8765/`）

---

## 3. 日常科研标准操作流（SOP）

### 场景 A：80% 日常推进（只和虚拟机里的 Claude Desktop 对话）
- **模型选择建议**：日常讨论、改代码、写脚本、整理结果，在 Claude Desktop 选择 **`Claude Sonnet 5.5`**（速度最快、额度最省）；只有立项定红线、解决尖锐审稿冲突、写核心论文段落时切 **`Claude Opus 5.5`**。
- Claude 写好实验脚本后，会保存到当前课题的 `03_代码与脚本/`（或对应实验目录），并告诉你脚本路径。
- 你切到宿主机 **Pebrel 终端**，手动回车运行该 Python 脚本（或把脚本放到 Colab 跑），把终端日志/结果保存回共享区，然后告诉 Claude：*“跑完了，结果在 `...`，请分析并更新 `_control/01_PROJECT_STATE.md`。”*

### 场景 B：20% 关键门禁会诊（呼叫 DeepSeek 红队 / GPT 顾问）
1. **在虚拟机 Claude Desktop 说**：
   > *“准备进行门禁会诊，请把核心背景、数据边界与 2 个具体问题浓缩到 1500 字以内，写入 `_bus/ask.md`。”*
2. **切到宿主机 Pebrel 终端手动点火**（进入当前课题目录，或加 `--project` 参数）：
   ```bash
   # 先预览发送给谁、读了多少字、走哪个模型（0 消耗，推荐习惯性先瞄一眼）
   consult all --dry-run

   # 只请 DeepSeek 红队挑刺（API 模型 ID: deepseek-flash）
   consult redteam

   # 只请本机 GPT 首席顾问深度裁决（可加 --effort xhigh）
   consult advisor

   # 顺序实时流式呼叫 GPT 顾问 + DeepSeek 红队（如需静默并发加 --no-stream）
   consult all
   ```
   *(如果想临时切其他模型做一次对比，无需改配置文件：`consult redteam --model deepseek-v4-pro`；`all` 模式下请写 `--model redteam=deepseek-v4-pro`。)*
3. **回虚拟机 Claude Desktop 收口拍板**：
   你只需要告诉 Claude 一句话：
   > *“会诊回执已到，请读取 `_bus/reply.md`，对照 `_control/02_DECISION_LEDGER.md` 过滤越权或无效建议，向我汇报 A/B 选项。”*
   你回复 **“选 A”** 或 **“选 B”** 后，Claude 将新决定记入 `02_DECISION_LEDGER.md` 并推进执行。

### 场景 C：使用 Gemini 4 文献阅读与宿主机辅佐（100% 零封号方案）
- **红线**：**绝对不要**把 Gemini CLI / Antigravity 的 OAuth 登录态挂进任何第三方反代或 `cli2api`。
- **安全用法**：直接打开你当前的 **Antigravity 官方原生客户端**，把工作区打开在 `$EXCHANGE_ROOT/02_公共资料库_Library`（或具体课题目录），让 `Gemini 4 Argon / Hydrogen` 批量吞吐几十篇 PDF、输出《文献事实卡片》存入课题目录或公共库，再由 Claude 读取使用。

### 场景 D：使用 GLM 5.3 / 5.3 Flash 做可编辑周报与 PPT（Track B，不污染 `_bus/`）
- **制作与多轮迭代可编辑 `.pptx`**：直接在宿主机终端运行 `ppt`（只写 `04_组会与周报_Weekly/`，自动注入 `academic-ppt-studio` 与 `00_CONTRACT.md`，并在跑 `build_pptx.py` 前做无密钥环境隔离与质量门禁检查）：
  ```bash
ppt --topic "<汇报主题>" --project "<课题目录名>"
# 可选参数：--dir <已有周报子目录>（接续迭代同一份 PPT） | --effort off|low|medium|high
```
- **纯文本润色或表格清洗**（写入 `_bus/reply.md`）：可用 `consult glm_fast`。

---

## 4. 模型角色与配置入口（以 `models.toml` 为唯一真源）

各外部角色默认模型、协议与端点均以 [models.toml](models.toml) 为唯一真源（可在宿主机运行 `consult all --dry-run` 查看实时状态）：
- **VM Claude Desktop**：日常推进用 `Sonnet`（关推理），立项/仲裁/定稿用 `Opus`（开推理），长篇润色用 `Fable`。
- **`advisor` (OpenAI GPT / `cc-switch`)**：`model = "auto"` 自动跟随本机 `cc-switch`（如 `gpt-6.2-astra`），支持 `--effort low|medium|high|xhigh`。
- **`redteam` (DeepSeek 官方 API)**：`model = "deepseek-flash"`（重型思考可切 `--model deepseek-v4-pro`）。
- **`glm_heavy` / `glm_fast` (第三方中转 `https://open.bigmodel.cn/api/paas/v4`)**：分别对应 `glm-5.3` / `glm-5.3-flash`（注意：涉及未发表契约时按需脱敏，主要推荐通过 `ppt` 用于 Track B 周报展示）。
- **Antigravity 原生 Gemini**：宿主机直接读取交换区，严禁把 OAuth 挂进第三方反代。

### 如何永久热更新默认模型？
打开 `00_全局中枢_Hub/models.toml` 修改对应角色的 `model = "..."` 一行即可：
- `[roles.redteam]`：`model = "deepseek-flash"`
- `[roles.advisor]`：`model = "auto"`
- `[roles.glm_heavy]`：`model = "glm-5.3"`
- `[roles.glm_fast]`：`model = "glm-5.3-flash"`

---

## 5. 如何开启第二个、第三个新科研课题？

1. 在宿主机 Pebrel 终端运行（**所有新课题永远建在 `01_在研课题_Projects/` 下**）：
   ```bash
   newproj "P02_你的新课题名称" --goal "一句话描述研究问题"
   # 若希望同时把新课题设为默认 active_project，追加 --activate：
   newproj "P02_你的新课题名称" --goal "..." --activate
   ```
2. 在虚拟机 Claude Desktop 左侧新建一个对应 Project（如 `[P02] 你的新课题名称`），在 Project 指令里把路径指向 `/Volumes/My Shared Files/ClaudeShield-Exchange/01_在研课题_Projects/P02_你的新课题名称`。
3. 之后两个课题可以同一天并行推进——各自有独立的 `_control/` 状态面板与 `_bus/` 会诊信箱，**100% 物理隔离不串台**！
4. 当某课题彻底结题不再推进时，直接把它在 `01_在研课题_Projects/` 下的整个文件夹拖入 `99_历史归档_Archive/` 并更新 `WORKSPACE_INDEX.md` 即可。

---

## 6. 全局共同 Skill 专区（`00_全局中枢_Hub/skills/`）使用指南

所有通用科研 Skill 统一存放在 `00_全局中枢_Hub/skills/`（已同步软链接至宿主机 Antigravity `~/.gemini/config/skills/`，虚拟机内 Claude Desktop 也可直接读取）。所有 Skill 均已剥离旧综述路径硬编码，采用**「总表引导 + 按需懒加载」**机制以防烧额度：

- **索引文件**：[SKILL_INDEX.md](skills/SKILL_INDEX.md)（共 17 个文件夹，全部已软链同步至 `~/.gemini/config/skills/`，涵盖方法与数据审计、论文写作精修、实验防泄漏、审稿 Rebuttal、顶刊投稿检查、科研绘图、可编辑 PPT 与新课题立项评估）。
- **向 Claude 发起任务的常用触发口令**：
  - 写正文/改稿去 AI 腔与去防御腔：*“先按 `SKILL_INDEX.md` 读取 `writing-core` 和 `anti-defensive-writing`，把这一节的消极免责改为正向范围界定。”*
  - 核对论文数字与代码是否一致：*“按 `source-grounded-method-audit` 与 `verification` 核查 `论文初稿.md` 中的指标和分母。”*
  - 画顶刊级数据图：*“按 `figures-python` 并导入 `00_全局中枢_Hub/plotting/academic_style.py` 编写画图脚本。”*
  - 做周报 PPT：*“按 `04_组会与周报_Weekly/README_周报写入规范.md` 和 `academic-ppt-studio` 准备本周四件套。”*
  - 开启新课题可行性自审：*“先读取 `idea-evaluator` 评估我的新课题方案。”*

---

## 7. 两大公共科研配置工具（论文体检 & 统一画图样式）

### 7.1 论文与报告自动化体检器（`check_manuscript.py`）
零第三方依赖，在宿主机 Pebrel 终端一键检查当前课题稿件的 **Markdown 相对断链**、**未清零占位符（TODO/待核）**、**`00_CONTRACT.md` 禁用夸大词**、**AI 套话/主观第一人称** 以及 **防御性自我贬低句式**：
```bash
# 默认检查当前课题（或 P01）的 论文初稿.md 与 研究论文目标与执行路线.md
checkms

# 检查任意指定的 Markdown 报告或周报讲稿文件
checkms --file "01_在研课题_Projects/<课题目录名>/02_实验与验证/<报告路径>.md"
```

### 7.3 通用命令参数速查（适用于任意课题）
- `p <关键词>` / `p01` / `p02`...：秒切对应课题目录（`newproj` 自动生成新课题前缀别名）。
- `consult <角色> --project "<课题>" --effort <档位> --eli5`：`<角色>` 可选 `redteam`、`advisor`、`all`、`glm_fast`；多角色用 `--model redteam=<模型ID>`。
- `runnotify colab run -s <会话名> --gpu <T4|L4|A100> --timeout 0 --keep "<脚本.py>" [脚本参数...]`：免网页跑 Colab；跑完后 `colab download -s <会话名> <远程结果> <本地目标>` 并 `colab stop -s <会话名>`。

### 7.2 全局顶刊绘图样式包（`00_全局中枢_Hub/plotting/academic_style.py`）
统一所有课题的出图规范（Okabe-Ito 色盲友好调色板、88 mm 单栏 / 180 mm 双栏标准宽、自动隐藏上右边框、Embedded TrueType 字体、一键同时导出矢量 PDF 与 450 DPI PNG）。详细调用示例见 `00_全局中枢_Hub/plotting/README.md`。

---

## 8. 每周组会汇报标准工作流（`04_组会与周报_Weekly/`）

每个课题舱下均设独立的 `04_组会与周报_Weekly/`（**Track B 下游交付线，对主线单向只读**）：
1. **建文件夹**：每次组会新建 `YYYY-MM-DD_汇报主题/`（绝不覆盖历史周报）。
2. **产四件套**：由 Claude / GLM-5.3 严格依据 `_control/01_PROJECT_STATE.md` 与已落盘实验生成 `01-周报大纲.md`、`02-逐页演讲稿.md`（每页含：核心结论、数据路径、逐字讲稿、边界 Q&A）、`build_pptx.py` 与 `周报-YYYY-MM-DD.pptx`。
3. **双检门禁**：交付前在宿主机运行 `checkms` 与 `check_pptx_quality.py` 均通过。
4. **会后回流**：组会后把导师反馈记入 `03-组会反馈与待办.md`，由 Claude 对照 `02_DECISION_LEDGER.md` 研判，**仅经 PI 拍板确认后**才升格写入主线台账。

---

## 9. 全局多课题科研总控大屏（`http://127.0.0.1:8765/`）

- **启动方式**：直接双击交换区根目录的 **`打开科研总控大屏.command`**（或在终端访问 `http://127.0.0.1:8765/`）。
- **设计特性**：零后台轮询任务、零冗余账本、**0 Token 消耗**，仅监听本机 `127.0.0.1`：
  1. **多课题自动切换**：自动发现 `01_在研课题_Projects/` 下的全部课题（`P01` 及后续新建的 `P02` 等），同屏渲染作战沙盘、决策台账、红线契约与主控守则；
  2. **跨端信箱与文件浏览器**：并排查看 `_bus/ask.md` / `_bus/reply.md` 及历史归档，在线阅读论文、实验与周报文档；
  3. **操作手册与高频口令一键复制台**：内置本手册全文与 8 条高频口令卡片（点击即复制并自动带入当前课题路径）；
  4. **网页一键体检 & 预览**：顶部支持直接点击触发 `checkms` 论文体检与 `consult --dry-run` 负载预览，并实时脱敏显示四大外脑模型的就绪状态。
- **停止服务命令**：`pkill -f "00_全局中枢_Hub/dashboard/serve.py"`
