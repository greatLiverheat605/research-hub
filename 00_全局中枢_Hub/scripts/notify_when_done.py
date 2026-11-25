#!/usr/bin/env python3
"""
00_全局中枢_Hub/scripts/notify_when_done.py
零 AI Token 消耗的实验完成自通知工具：
1) runnotify <命令...> : 包装本地实验命令，跑完后自动弹出 macOS 系统通知并响铃。
2) watchdone <文件路径> : 静默监听 Colab/Drive 同步落地的结果文件，出现且写稳后立即弹窗通知。
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _notify(title: str, message: str, sound: str = "Glass") -> None:
    safe_title = title.replace('"', '\\"')
    safe_msg = message.replace('"', '\\"')
    script = f'display notification "{safe_msg}" with title "{safe_title}" sound name "{sound}"'
    subprocess.run(["/usr/bin/osascript", "-e", script], check=False)
    sys.stdout.write("\a")
    sys.stdout.flush()


def cmd_run(command_args: list) -> int:
    if not command_args:
        print("用法: notify_when_done.py run <命令及参数>", file=sys.stderr)
        return 2
    started = time.time()
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    cwd = Path.cwd()
    log_path = cwd / f"run_{ts}.log"
    manifest_path = cwd / f"run_manifest_{ts}.json"

    print(f"[runnotify] 开始执行: {' '.join(command_args)}", flush=True)
    print(f"[runnotify] 日志保存至: {log_path}", flush=True)

    output_lines = []
    proc = subprocess.Popen(
        command_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        bufsize=1,
    )
    with log_path.open("w", encoding="utf-8") as lf:
        if proc.stdout is not None:
            for line in proc.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                lf.write(line)
                output_lines.append(line)
    proc.wait()
    elapsed = round(time.time() - started, 1)
    combined = "".join(output_lines[-200:])
    complete_ok = "COMPLETE_OK" in combined

    git_sha = ""
    g_res = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True)
    if g_res.returncode == 0:
        git_sha = g_res.stdout.strip()

    manifest = {
        "timestamp": ts,
        "cwd": str(cwd),
        "command": command_args,
        "git_sha": git_sha,
        "exit_code": proc.returncode,
        "complete_ok_detected": complete_ok,
        "elapsed_s": elapsed,
        "log_file": log_path.name,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[runnotify] 运行清单已保存: {manifest_path.name} (COMPLETE_OK={complete_ok})", flush=True)
    try:
        import datetime as _dt
        import exp_store
        t0 = _dt.datetime.strptime(ts, "%Y%m%d_%H%M%S")
        started_str = t0.strftime("%Y-%m-%d %H:%M:%S")
        finished_str = (t0 + _dt.timedelta(seconds=elapsed)).strftime("%Y-%m-%d %H:%M:%S")
        cmd_str = " ".join(str(x) for x in command_args)
        action = "colab_exec" if "colab" in cmd_str else "runnotify"
        status_str = "SUCCESS" if (proc.returncode == 0 and complete_ok) else ("FAILED" if proc.returncode != 0 else "DONE")
        exp_store.record_dispatch_run(
            project_dir=cwd,
            job_id=f"run_{ts}",
            action=action,
            status=status_str,
            started_at=started_str,
            finished_at=finished_str,
            job_payload={"script": log_path.name, "command": command_args},
            detail=f"Command: `{cmd_str}` | elapsed: {elapsed}s | COMPLETE_OK={complete_ok}\n" + "".join(output_lines[-15:]),
        )
    except Exception:
        pass

    cmd_name = Path(command_args[0]).name
    if proc.returncode == 0:
        tag = "且检测到 COMPLETE_OK" if complete_ok else "（未检测到 COMPLETE_OK 标记）"
        msg = f"{cmd_name} 已结束（耗时 {elapsed}s）{tag}"
        _notify("ResearchHub 实验完成", msg, "Glass")
    else:
        msg = f"{cmd_name} 异常退出（code={proc.returncode}，耗时 {elapsed}s），请查看 {log_path.name}"
        _notify("ResearchHub 实验报错", msg, "Basso")
    return proc.returncode


def cmd_watch(target_path: str, interval: float, timeout: float, sync_into: str = "") -> int:
    target = Path(target_path).expanduser().resolve()
    print(f"[watchdone] 正在 0 消耗监听结果落地: {target}", flush=True)
    if sync_into:
        print(f"[watchdone] 落地后将自动复制到交换区: {sync_into}", flush=True)
    print(f"[watchdone] 轮询间隔: {interval}s | 按 Ctrl+C 取消", flush=True)
    started = time.time()
    last_size = -1
    stable_hits = 0

    while True:
        if target.exists() and target.is_file():
            cur_size = target.stat().st_size
            if cur_size > 0 and cur_size == last_size:
                stable_hits += 1
                if stable_hits >= 2:
                    elapsed = round(time.time() - started, 1)
                    copied_note = ""
                    if sync_into:
                        dest_dir = Path(sync_into).expanduser().resolve()
                        dest_dir.mkdir(parents=True, exist_ok=True)
                        dest_file = dest_dir / target.name
                        shutil.copy2(target, dest_file)
                        copied_note = f" 并已复制入交换区 {dest_file.name}"
                    msg = f"结果文件已落地: {target.name} ({cur_size} bytes, 等待 {elapsed}s){copied_note}"
                    print(f"\n✅ [watchdone] {msg}")
                    _notify("ResearchHub 结果已同步", msg, "Glass")
                    return 0
            else:
                last_size = cur_size
                stable_hits = 0
        if timeout > 0 and (time.time() - started) > timeout:
            print(f"\n⏰ [watchdone] 超过等待上限 {timeout}s，已退出。")
            return 1
        time.sleep(interval)


def main() -> int:
    parser = argparse.ArgumentParser(description="零 AI Token 实验完成自通知工具")
    sub = parser.add_subparsers(dest="mode", required=True)

    p_run = sub.add_parser("run", help="包装并运行本地实验命令，完成后弹窗通知")
    p_run.add_argument("cmd", nargs=argparse.REMAINDER, help="要执行的命令及参数")

    p_watch = sub.add_parser("watch", help="监听 Colab/Drive 同步文件落地，完成后弹窗通知")
    p_watch.add_argument("path", help="待监听的目标文件路径")
    p_watch.add_argument("--into", default="", help="可选：落地后自动复制到交换区目标目录，供虚拟机 Claude 直接读取")
    p_watch.add_argument("--interval", type=float, default=5.0, help="检查间隔秒数（默认 5.0）")
    p_watch.add_argument("--timeout", type=float, default=0.0, help="超时秒数（0 表示不超时）")

    args = parser.parse_args()
    if args.mode == "run":
        cmd = args.cmd
        if cmd and cmd[0] == "--":
            cmd = cmd[1:]
        return cmd_run(cmd)
    return cmd_watch(args.path, args.interval, args.timeout, args.into)


if __name__ == "__main__":
    sys.exit(main())
