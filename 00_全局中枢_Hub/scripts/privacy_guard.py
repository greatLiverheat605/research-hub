#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/privacy_guard.py
公开仓库隐私与本地绝对路径零泄漏守卫（可作为 git pre-commit / pre-push 钩子或独立扫描器）：
- 拦截任何宿主机个人路径 (/Users/<username>)、Tailscale 私网域名 (*.ts.net)、设备节点名、邮箱及 API Token 明文。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Tuple

FORBIDDEN_PATTERNS: List[Tuple[str, str]] = [
    (r"/Users/[A-Za-z0-9_.-]+", "macOS 个人主目录绝对路径 (/Users/...)"),
    (r"C:\\Users\\[A-Za-z0-9_.-]+", "Windows 个人主目录绝对路径"),
    (r"[A-Za-z0-9_-]+\.tail[A-Za-z0-9]+\.ts\.net", "Tailscale 私网域名 (*.ts.net)"),
    (r"iv-yehklpaadcf8b8x8cob7|jx202609040258h|dengqimacbook|diquizzer", "私有节点或账户标识"),
    (r"1205295471@qq\.com|privaterelay\.appleid\.com", "协作者私人邮箱"),
    (r"\b(?:sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,})\b", "疑似 API Key / Token 明文"),
]

SKIP_DIRS = {".git", "__pycache__", "node_modules"}
SKIP_EXTS = {".sqlite", ".sqlite-wal", ".sqlite-shm", ".png", ".jpg", ".jpeg", ".pdf", ".pptx", ".zip"}


def scan_file(path: Path) -> List[str]:
    if path.suffix.lower() in SKIP_EXTS or not path.is_file():
        return []
    if path.name in ("privacy_guard.py", "vm_claude_config.bak.json", "vm_claude_config_repaired.json"):
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    hits: List[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for pat, desc in FORBIDDEN_PATTERNS:
            if re.search(pat, line):
                hits.append(f"{path}:{lineno}: 命中 [{desc}] -> {line.strip()[:120]}")
    return hits


def scan_repo(repo_dir: Path, staged_only: bool = False) -> List[str]:
    all_hits: List[str] = []
    if staged_only and (repo_dir / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
        )
        files = [repo_dir / f.strip() for f in res.stdout.splitlines() if f.strip()]
        for fp in files:
            all_hits.extend(scan_file(fp))
        return all_hits

    for root, dirs, files in repo_dir.walk() if hasattr(Path, "walk") else _walk_fallback(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            all_hits.extend(scan_file(Path(root) / fn))
    return all_hits


def _walk_fallback(repo_dir: Path):
    import os
    for r, d, f in os.walk(repo_dir):
        yield Path(r), d, f


def main() -> int:
    parser = argparse.ArgumentParser(description="公开仓库隐私与本地路径零泄漏扫描器")
    parser.add_argument("target", nargs="?", default=".", help="要扫描的仓库或目录路径")
    parser.add_argument("--staged", action="store_true", help="仅扫描 Git 暂存区 (pre-commit 模式)")
    args = parser.parse_args()

    target_dir = Path(args.target).resolve()
    hits = scan_repo(target_dir, staged_only=args.staged)
    if hits:
        print("❌ [PrivacyGuard] 检测到以下隐私/本地路径泄漏，已拦截提交：", file=sys.stderr)
        for h in hits[:30]:
            print(f"  - {h}", file=sys.stderr)
        return 1
    print(f"✅ [PrivacyGuard] 扫描通过（0 隐私/本地绝对路径泄漏）: {target_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
