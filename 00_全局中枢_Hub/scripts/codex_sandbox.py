#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/codex_sandbox.py
限制性 Codex CLI 启动封装器：
强制把 codex 的 workspace-write 沙箱锁死在
`01_在研课题_Projects/<课题>/03_代码与脚本/<独立子模块>/` 之内，
严禁在课题根目录、_control/、01_论文与协议/、04_组会与周报_Weekly/ 或 冻结存档-* 内启用写权限。
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = HUB_DIR.parent
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return os.path.commonpath([str(child), str(parent)]) == str(parent)
    except ValueError:
        return False


def validate_codex_workdir(target_dir: Path) -> Path:
    resolved = target_dir.expanduser().resolve()
    projects_resolved = PROJECTS_DIR.resolve()
    if not _is_within(resolved, projects_resolved) or resolved == projects_resolved:
        raise PermissionError(
            f"安全拦截：codexbox 仅允许在 {PROJECTS_DIR}/<课题>/03_代码与脚本/<子模块>/ 下运行（当前：{resolved}）"
        )

    rel_parts = resolved.relative_to(projects_resolved).parts
    # Must be at least: (project_name, "03_代码与脚本", "<submodule_dir>", ...)
    if len(rel_parts) < 3 or rel_parts[1] != "03_代码与脚本":
        raise PermissionError(
            "安全拦截：禁止在课题根目录、_control/、01_论文与协议/ 或 04_组会与周报_Weekly/ 直接开启 Codex 写权限。\n"
            f"请先在 03_代码与脚本/ 下建一个独立子目录，例如：{projects_resolved / rel_parts[0] / '03_代码与脚本' / 'exp_submodule'}"
        )
    if "冻结存档-20261117" in rel_parts or "冻结件" in rel_parts:
        raise PermissionError(f"安全拦截：禁止对只读冻结目录开启 Codex 写权限：{resolved}")
    return resolved


def find_codex_bin() -> Optional[str]:
    found = shutil.which("codex")
    if found:
        return found
    fallback = Path.home() / ".nvm" / "versions" / "node" / "v24.21.0" / "bin" / "codex"
    if fallback.is_file():
        return str(fallback)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="codexbox",
        description="受限沙箱 Codex CLI 启动器（只允许写 03_代码与脚本/<子模块>/）",
    )
    parser.add_argument("workdir", help="允许 Codex 写入的独立实验子目录（必须在 03_代码与脚本/ 下至少一级）")
    parser.add_argument("--dry-run", action="store_true", help="仅校验目录安全边界并打印将执行的 codex 命令，不启动")
    args, extra = parser.parse_known_args()

    try:
        safe_dir = validate_codex_workdir(Path(args.workdir))
    except PermissionError as err:
        print(f"❌ {err}", file=sys.stderr)
        return 1

    codex_bin = find_codex_bin()
    if not codex_bin:
        print("❌ 未找到 codex CLI 可执行文件。", file=sys.stderr)
        return 1

    if extra and extra[0] == "--":
        extra = extra[1:]

    blocked_prefixes = (
        "--dangerously",
        "-s",
        "--sandbox",
        "--add-dir",
        "-c",
        "--config",
        "-C",
        "--cd",
        "--full-auto",
    )
    for tok in extra:
        low = tok.strip().lower()
        if low.startswith("--dangerously"):
            print(
                f"❌ 安全拦截：codexbox 禁止透传沙箱/目录越权参数 `{tok}`。",
                file=sys.stderr,
            )
            return 1
        for pref in blocked_prefixes:
            if low == pref or low.startswith(pref + "="):
                print(
                    f"❌ 安全拦截：codexbox 禁止透传沙箱/目录越权参数 `{tok}`。",
                    file=sys.stderr,
                )
                return 1

    if args.dry_run:
        print(f"🔒 [codexbox] 写权限已锁定在独立子目录: {safe_dir}")
        print(f"▶ 命令: {codex_bin} -C {safe_dir} -s workspace-write {' '.join(extra)}")
        return 0

    safe_dir.mkdir(parents=True, exist_ok=True)
    cmd = [codex_bin, "-C", str(safe_dir), "-s", "workspace-write", *extra]
    print(f"🔒 [codexbox] 写权限已锁定在独立子目录: {safe_dir}")
    print(f"▶ 命令: {' '.join(cmd)}")

    proc = subprocess.run(cmd)
    print("\n👉 提醒：Codex 运行结束后，请让虚拟机 Claude Desktop 读取并验收该子目录产物后再登记进 _control/02_DECISION_LEDGER.md。")
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
