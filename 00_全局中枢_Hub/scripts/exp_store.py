#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/exp_store.py
本地 SQLite 结构化实验与任务记忆库（灵感源自 OpenResearch SQLite Experiment Store）：
- 记录每次 _dispatch 执行的课题、动作、脚本、参数、指标（含 COMPLETE_OK 解析）、产物路径与 Git SHA；
- 支持从历史 _dispatch/done/ + _dispatch/archive/ 回填；
- 供 dashboard/serve.py (/api/experiments) 与 CLI 快速检索。
"""

import argparse
import datetime
import json
import re
import sqlite3
import subprocess
import sys
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_DB_LOCK = threading.Lock()

SCRIPTS_DIR = Path(__file__).resolve().parent
HUB_DIR = SCRIPTS_DIR.parent
EXCHANGE_ROOT = Path(os.environ.get("RH_EXCHANGE_ROOT", HUB_DIR.parent))
PROJECTS_DIR = EXCHANGE_ROOT / "01_在研课题_Projects"
DB_PATH = EXCHANGE_ROOT / "00_全局中枢_Hub" / "experiments.sqlite"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    job_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    duration_sec REAL,
    script_path TEXT DEFAULT '',
    params_json TEXT DEFAULT '{}',
    metrics_json TEXT DEFAULT '{}',
    artifacts_json TEXT DEFAULT '[]',
    git_sha TEXT DEFAULT '',
    summary TEXT DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_runs_project_time ON runs(project, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_uniq ON runs(project, job_id, started_at);
"""


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.executescript(SCHEMA_SQL)
    return conn


def _detect_git_sha(project_dir: Path) -> str:
    for candidate in (project_dir, project_dir / "03_代码与脚本", project_dir / "05_Gitea协作同步区/repo"):
        if (candidate / ".git").exists():
            res = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
    return ""


def _parse_metrics_from_detail(detail: str) -> Dict[str, Any]:
    """从日志末尾提取 COMPLETE_OK {...} 或 key=value 指标。"""
    metrics: Dict[str, Any] = {}
    for line in detail.splitlines():
        line_s = line.strip()
        if "COMPLETE_OK" in line_s:
            m = re.search(r"COMPLETE_OK\s*(\{.*\})", line_s)
            if m:
                try:
                    parsed = json.loads(m.group(1))
                    if isinstance(parsed, dict):
                        metrics.update(parsed)
                        continue
                except Exception:
                    pass
            for k, v in re.findall(r"([A-Za-z0-9_.-]+)=([+-]?\d+(?:\.\d+)?(?:e[+-]?\d+)?)", line_s):
                try:
                    metrics[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
                except Exception:
                    metrics[k] = v
    return metrics


def _calc_duration(started_at: str, finished_at: str) -> Optional[float]:
    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        t0 = datetime.datetime.strptime(started_at, fmt)
        t1 = datetime.datetime.strptime(finished_at, fmt)
        return round((t1 - t0).total_seconds(), 2)
    except Exception:
        return None


def record_dispatch_run(
    project_dir: Path,
    job_id: str,
    action: str,
    status: str,
    started_at: str,
    finished_at: str,
    job_payload: Optional[Dict[str, Any]] = None,
    detail: str = "",
    db_path: Path = DB_PATH,
) -> int:
    payload = job_payload or {}
    script_path = str(payload.get("script") or payload.get("body_file") or payload.get("file") or "")
    artifacts: List[str] = []
    for k in ("pull_files", "artifacts"):
        val = payload.get(k)
        if isinstance(val, list):
            artifacts.extend(str(x) for x in val)
    if payload.get("file"):
        artifacts.append(str(payload["file"]))

    params = {
        k: v
        for k, v in payload.items()
        if k not in ("job_id", "action")
    }
    metrics = _parse_metrics_from_detail(detail)
    summary_lines = [ln.strip() for ln in detail.strip().splitlines() if ln.strip()]
    summary = " | ".join(summary_lines[:3])[:300]
    dur = _calc_duration(started_at, finished_at)
    sha = _detect_git_sha(project_dir)

    with _DB_LOCK:
        with get_conn(db_path) as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO runs (
                    project, job_id, action, status, started_at, finished_at, duration_sec,
                    script_path, params_json, metrics_json, artifacts_json, git_sha, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_dir.name,
                    job_id,
                    action,
                    status,
                    started_at,
                    finished_at,
                    dur,
                    script_path,
                    json.dumps(params, ensure_ascii=False),
                    json.dumps(metrics, ensure_ascii=False),
                    json.dumps(artifacts, ensure_ascii=False),
                    sha,
                    summary,
                ),
            )

            return cursor.rowcount


def query_runs(project: Optional[str] = None, limit: int = 50, db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    with get_conn(db_path) as conn:
        if project:
            rows = conn.execute(
                "SELECT * FROM runs WHERE project = ? OR project LIKE ? ORDER BY id DESC LIMIT ?",
                (project, f"{project}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        for jf in ("params_json", "metrics_json", "artifacts_json"):
            key = jf.replace("_json", "")
            try:
                d[key] = json.loads(d.pop(jf) or "{}")
            except Exception:
                d[key] = {}
        out.append(d)
    return out


def backfill_from_done(projects_dir: Path = PROJECTS_DIR, db_path: Path = DB_PATH) -> int:
    """从各课题既有 _dispatch/done/*_result.md 和 archive/*.json 导入历史记录。"""
    imported = 0
    if not projects_dir.is_dir():
        return 0
    for proj in sorted(projects_dir.iterdir()):
        if not proj.is_dir() or proj.name.startswith("."):
            continue
        done_dir = proj / "_dispatch" / "done"
        arch_dir = proj / "_dispatch" / "archive"
        for md_file in sorted(done_dir.glob("*_result.md")):
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            job_id = md_file.stem.removesuffix("_result")
            action_m = re.search(r"\*\*白名单动作\*\*[：:]\s*`([^`]+)`", text)
            status_m = re.search(r"\*\*状态\*\*[：:]\s*\*\*([A-Z]+)\*\*", text)
            time_m = re.search(r"\*\*开始 / 结束\*\*[：:]\s*([0-9:-]+ [0-9:]+)\s*→\s*([0-9:-]+ [0-9:]+)", text)
            detail_m = re.search(r"## 执行详情\s*\n(.*)", text, flags=re.S)
            action = action_m.group(1) if action_m else "unknown"
            status = status_m.group(1) if status_m else "UNKNOWN"
            started = time_m.group(1) if time_m else "1970-01-01 00:00:00"
            finished = time_m.group(2) if time_m else started
            detail = detail_m.group(1).strip() if detail_m else ""

            payload: Dict[str, Any] = {}
            arch_json = arch_dir / f"{job_id}.json"
            if arch_json.is_file():
                try:
                    payload = json.loads(arch_json.read_text(encoding="utf-8"))
                except Exception:
                    payload = {}

            imported += record_dispatch_run(proj, job_id, action, status, started, finished, payload, detail, db_path=db_path)

        # 同时回填 runnotify 生成的 run_manifest_*.json
        for mf in sorted(proj.glob("run_manifest_*.json")):
            try:
                mdata = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:
                continue
            ts = str(mdata.get("timestamp") or mf.stem.replace("run_manifest_", ""))
            if len(ts) == 15 and "_" in ts:
                started = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}:{ts[13:15]}"
            else:
                started = ts
            dur = float(mdata.get("elapsed_s") or 0.0)
            try:
                t0 = datetime.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
                finished = (t0 + datetime.timedelta(seconds=dur)).strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                finished = started
            cmd_list = mdata.get("command") or []
            cmd_str = " ".join(str(x) for x in cmd_list)
            action = "colab_exec" if "colab" in cmd_str else "runnotify"
            status = "FAILED" if mdata.get("exit_code") != 0 else ("SUCCESS" if mdata.get("complete_ok_detected") else "DONE")
            log_rel = str(mdata.get("log_file") or "")
            detail = f"Command: `{cmd_str}` | elapsed: {dur}s | COMPLETE_OK={mdata.get('complete_ok_detected')}"
            if log_rel and Path(log_rel).name == log_rel and not (proj/log_rel).is_symlink() and (proj / log_rel).is_file():
                with (proj/log_rel).open("rb") as f:
                    f.seek(max(0,os.fstat(f.fileno()).st_size-16384))
                    tail = f.read().decode("utf-8", "replace").splitlines()[-15:]
                detail += "\n" + "\n".join(tail)
            imported += record_dispatch_run(
                proj,
                f"run_{ts}",
                action,
                status,
                started,
                finished,
                {"script": log_rel or cmd_str, "command": cmd_list},
                detail,
                db_path=db_path,
            )
    return imported


def main() -> int:
    parser = argparse.ArgumentParser(description="ResearchHub SQLite 实验记忆库查询与回填")
    parser.add_argument("--backfill", action="store_true", help="从既有 _dispatch/done/ 导入历史记录")
    parser.add_argument("--project", help="按课题前缀过滤，如 P01 或 P02")
    parser.add_argument("--limit", type=int, default=20, help="返回条数（默认 20）")
    args = parser.parse_args()

    if args.backfill:
        n = backfill_from_done()
        print(f"[ExpStore] 历史回填完成，新增 {n} 条记录（库文件：{DB_PATH}）。")
    rows = query_runs(project=args.project, limit=args.limit)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
