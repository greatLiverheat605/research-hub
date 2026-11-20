#!/usr/bin/env python3
"""Research Hub CLI quick reference (`hubhelp`).

Prints categorized commands, active projects, Ghostty/Pebrel shortcuts, and
role endpoints without consuming any AI tokens or printing secrets.
"""

import argparse
import os
from pathlib import Path
import sys

HUB_DIR = Path(__file__).resolve().parent.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"


def _supports_color():
    return sys.stdout.isatty() and os.environ.get("NO_COLOR", "") == ""


def _c(code, text, color_on):
    if not color_on:
        return text
    return "\033[{}m{}\033[0m".format(code, text)


def _list_projects():
    if not PROJECTS_DIR.is_dir():
        return []
    return sorted(
        p.name for p in PROJECTS_DIR.iterdir() if p.is_dir() and not p.name.startswith(".")
    )


def main():
    parser = argparse.ArgumentParser(
        prog="hubhelp",
        description="科研流 CLI 指令与 Ghostty / Pebrel 快捷方式速查",
    )
    parser.add_argument(
        "topic",
        nargs="?",
        default="all",
        choices=["all", "nav", "consult", "tools", "ghostty", "pebrel"],
        help="可选分类过滤: all | nav | consult | tools | ghostty | pebrel",
    )
    args = parser.parse_args()
    color_on = _supports_color()

    projects = _list_projects()
    proj_hint = " | ".join(projects) if projects else "暂无课题"

    print(_c("1;36", "============================================================", color_on))
    print(_c("1;36", "  ResearchHub 科研中枢 CLI 速查 (Ghostty / Pebrel 通用)", color_on))
    print(_c("1;36", "============================================================", color_on))
    print("当前在研课题: {}".format(_c("1;33", proj_hint, color_on)))
    print("监控总控大屏: http://127.0.0.1:8765/  (命令: dash)")
    print()

    nav_rows = [
        (p.split("_", 1)[0].lower(), f"秒切 {p} 目录")
        for p in projects
        if "_" in p
    ]
    nav_rows.extend([
        ("p <关键词>", "模糊秒切任意课题目录（如 p 02 / p nc / p 三维）"),
        ("hub", "秒切 00_全局中枢_Hub 全局中枢目录"),
        ("dash", "启动并在浏览器打开科研总控大屏 (8765)"),
        ("hubhelp [分类]", "查看本速查 (分类: nav / consult / tools / ghostty / pebrel)"),
    ])

    sections = [
        (
            "nav",
            "1. 目录跳转与大屏",
            nav_rows,
        ),
        (
            "consult",
            "2. 跨端专家会诊 (consult.py)",
            [
                ('consult all --dry-run [--project "<课题名>"]', "0 消耗预演：查看字数、模型来源与端点"),
                ('consult redteam [--project "<课题名>"]', "仅呼叫 DeepSeek 红队 (deepseek-flash)"),
                ('consult advisor [--project "<课题名>"]', "仅呼叫本机 cc-switch GPT 顾问"),
                ('consult all [--project "<课题名>"]', "流式呼叫 DeepSeek 红队 + GPT 顾问（--no-stream 并发）"),
                ('consult glm_fast [--project "<课题名>"]', "呼叫 GLM API GLM-5.3-Flash 快速润色/整理"),
                ('consult <角色> --model [角色=]模型ID', "临时覆盖模型（all 模式需写 redteam=模型ID）"),
                ("consult <角色> --effort <off|low|medium|high|xhigh>", "切换思考推理档位"),
                ("consult <角色> --eli5", "附带大白话/ELI5 通俗解释"),
                ("consult <角色> --no-stream", "关闭实时思考流式输出，改为静默并发等待"),
            ],
        ),
        (
            "tools",
            "3. 体检、Colab、PPT 与配置管理",
            [
                ('checkms [--project "<课题名>"] [--file <路径>]', "检查论文/周报断链、待核占位符、禁用词与 AI 腔"),
                ('ppt --topic "<主题>" [--project "<课题名>"]', "GLM 多轮可编辑 PPT 引擎 (调 academic-ppt-studio + 质检)"),
                ("runnotify <本地命令或 colab run ...>", "0 消耗包装实验，跑完弹窗并生成 run_manifest_*.json"),
                ("colab run -s <会话> --gpu <T4|L4|A100> --timeout 0 --keep <脚本.py> [参数]", "免网页租用 Colab GPU 直接运行本地脚本"),
                ("colab download -s <会话> <远程文件> <本地文件> && colab stop -s <会话>", "拉回产物到课题目录并停止 GPU 计费"),
                ('watchdone "<Drive结果路径>" --into "<课题目标目录>"', "0 消耗监听 Drive 同步落地，自动搬入并弹窗"),
                ('codexbox "<课题>/03_代码与脚本/<子模块>"', "受限启动 Codex CLI（写权限锁死在实验子目录内）"),
                ('newproj "P0X_课题名" --goal "<研究目标>"', "新建物理隔离课题舱并自动生成 p0X 秒切别名"),
                ("hubkeys / hubmodels / ghosttycfg", "打开宿主机密钥 / models.toml / Ghostty 配置"),
                ("source ~/.zshrc", "刷新当前终端别名（~/.local/bin 内命令免 source 可直接用）"),
            ],
        ),
        (
            "ghostty",
            "4. Ghostty 专属操作与快捷键 (1.4.0)",
            [
                ("Cmd+T / Cmd+N", "新建标签页 / 新建 Ghostty 窗口"),
                ("Cmd+D / Cmd+Shift+D", "左右分屏 / 上下分屏 (Cmd+Shift+Enter 放大/还原分屏)"),
                ("Cmd+Opt+方向键  或  Cmd+[ / ]", "在分屏之间切换焦点"),
                ("Cmd+Shift+P", "打开 Ghostty 内置命令面板"),
                ("Cmd+F  /  Cmd+上/下", "搜索历史 / 按命令提示符快速跳转"),
                ("Cmd+,  /  Cmd+Shift+,", "打开配置 (ghosttycfg) / 热重载配置"),
                ("ghostty +validate-config", "校验 ~/.config/ghostty/config 语法是否合法"),
                ("ghostty +list-keybinds --default", "查看 Ghostty macOS 全部默认快捷键"),
            ],
        ),
        (
            "pebrel",
            "5. Pebrel 专属操作",
            [
                ("pview <文件.md>", "用 Pebrel GPU 渲染器阅读 Markdown (如 pview _bus/reply.md)"),
                ("open -e ~/Library/Application\\ Support/Pebrel/pebrel.luau", "编辑 Pebrel Profiles"),
                ('"/Applications/Pebrel.app/Contents/MacOS/pebrel" config check', "校验 pebrel.luau"),
            ],
        ),
    ]

    for key, title, rows in sections:
        if args.topic not in ("all", key):
            continue
        print(_c("1;32", title, color_on))
        for cmd, desc in rows:
            print("  {}  {}".format(_c("1", f"{cmd:<46}", color_on), desc))
        print()


if __name__ == "__main__":
    main()
