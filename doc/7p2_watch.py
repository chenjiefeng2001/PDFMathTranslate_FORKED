"""独立心跳 watcher：附加到已在运行的翻译进程上。

为什么必须独立进程
------------------
上一轮把 watcher 线程放在 launcher 里，launcher 退出后线程随之消失 —— 而
launcher 本来就该"发完就退"。实测：运行早已完成（``run-summary.json`` 存在、
退出码 0），而 ``heartbeat.json`` 从未出现，看起来像挂了 40 分钟。

所以这里是一个**只读、附加式**的进程：给它一个 pid 和一个日志路径，它自己轮询，
自己写心跳，自己在目标退出后收尾。它不启动、不杀死、不修改任何东西。

用法::

    python doc/7p2_watch.py --out <dir>
    python doc/7p2_watch.py --out <dir> --foreground   # 前台，便于调试
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def pid_alive(pid: int) -> bool:
    """Windows 上 ``os.kill(pid, 0)`` 抛 SystemError（WinError 87），用 tasklist。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return str(pid) in out.stdout
    except Exception:  # noqa: BLE001
        return False


def write_hb(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def classify(log_text: str, prev: str) -> tuple[str, str | None]:
    """从日志里粗判阶段。返回 ``(phase, highlight)``。

    阶段只是给人看的；真正的停滞判定靠 ``idle_s``（日志多久没变），与这里无关。

    判断顺序 = 阶段**时序倒序**（最晚的可能阶段排在最前）。日志是累积的，一旦某个
    标记出现过就永远在文本里，所以"先命中先返回"会把后续阶段永久遮住：实测一整轮
    50 页真跑全程只显示 ``reparse_recovered``，``render`` 阶段从未出现过。
    """
    if "parse dump" in log_text or "render plan dump" in log_text:
        return "render", None
    if "translating blocks" in log_text or "translate_stream" in log_text:
        n = log_text.count("it]")
        return "translate", (f"~{n} progress lines" if n else None)
    if "re-parsed cleanly" in log_text or "quarantine lifted" in log_text:
        n = log_text.count("re-parsed cleanly")
        return "reparse_recovered", (f"{n} collapsed page(s) recovered" if n else None)
    if "merged source pages" in log_text or "并进了同一" in log_text:
        # 注意：并页**不等于**降级到 legacy 引擎。曾经这里返回 "degraded_to_legacy"，
        # 既与事实不符（走的是 magicpdf，只是先隔离再重解析），又因为排在判断前面
        # 而把后续阶段整个遮住 —— 害我差点误判整轮跑废了。
        line = next(
            (
                ln
                for ln in log_text.splitlines()
                if "merged source pages" in ln or "并进了同一" in ln
            ),
            None,
        )
        return "merged_pages_detected", (line or "")[:160]
    if "page slice" in log_text:
        return "parse", None
    return "starting", None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--every", type=int, default=15)
    ap.add_argument("--foreground", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out)
    hb = out_dir / "heartbeat.json"
    pid_f = out_dir / "runner.pid"
    log = out_dir / "run.log"
    if not pid_f.is_file():
        print(f"no runner.pid in {out_dir}")
        return 2
    pid = int(pid_f.read_text(encoding="utf-8").strip() or 0)

    def _run() -> int:
        t0 = time.time()
        last, last_change = "", time.time()
        # 立刻写一份，调用方马上能看到"watcher 活着"
        write_hb(
            hb,
            {
                "phase": "attaching",
                "target_pid": pid,
                "elapsed_s": 0,
                "idle_s": 0,
                "stalled": False,
            },
        )
        while True:
            alive = pid_alive(pid)
            try:
                txt = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt = ""
            if txt != last:
                last, last_change = txt, time.time()
            if not alive:
                summary = out_dir / "run-summary.json"
                exit_code = None
                if summary.is_file():
                    try:
                        exit_code = json.loads(summary.read_text(encoding="utf-8")).get(
                            "exit"
                        )
                    except Exception:  # noqa: BLE001
                        exit_code = None
                write_hb(
                    hb,
                    {
                        "phase": "done",
                        "target_pid": pid,
                        "exit": exit_code,
                        "elapsed_s": round(time.time() - t0, 1),
                        "idle_s": round(time.time() - last_change, 1),
                        "stalled": False,
                        "note": "target process exited",
                    },
                )
                return 0
            phase, hl = classify(txt, last)
            idle = round(time.time() - last_change, 1)
            write_hb(
                hb,
                {
                    "phase": phase,
                    "target_pid": pid,
                    "elapsed_s": round(time.time() - t0, 1),
                    "idle_s": idle,
                    "stalled": idle > 420,
                    "log_bytes": len(txt),
                    "highlight": hl,
                },
            )
            time.sleep(args.every)

    if args.foreground:
        return _run()
    # 后台：输出落文件，不继承调用方句柄（否则调用方永远等不到 EOF）
    o = open(out_dir / "watch.out", "w", encoding="utf-8")
    e = open(out_dir / "watch.err", "w", encoding="utf-8")
    subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(Path(__file__).resolve()),
            "--out",
            str(out_dir),
            "--every",
            str(args.every),
            "--foreground",
        ],
        stdout=o,
        stderr=e,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    o.close()
    e.close()
    print(f"watcher detached -> {hb}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
