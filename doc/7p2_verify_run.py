"""对照实验的运行器：心跳 + 后台执行，取代"直接跑一条长命令"。

为什么不用直接执行
------------------
一次 50 页真跑要 30~90 分钟。把这种命令直接交给 shell，调用方只能等；而一旦
``opencode serve`` 之类的常驻进程持有控制台句柄，还会**看起来像挂起**（上一轮就
误判过一次，其实基准早已打印完）。

这里把三件事彻底拆开：
1. 常驻服务单独起，输出**写文件**（不继承调用方任何句柄）；
2. 翻译跑在**独立后台进程**里，逐段写 JSONL/JSON 心跳；
3. 调用方只轮询心跳，因此无论跑 30 秒还是 90 分钟都不会被挂住。

心跳里区分三种状态，避免"还在跑"和"卡住了"混为一谈：
- ``phase: parse`` / ``translate`` —— 正常推进；
- ``done_calls`` 长时间不变而 ``elapsed_s`` 在涨 —— **停滞**（服务没响应）；
- ``phase: done`` 且进程退出 —— 结束。

用法::

    python doc/7p2_verify_run.py serve-start
    python doc/7p2_verify_run.py launch --out <dir> --pages 1-50 --thread 4 [--ignore-cache]
    python doc/7p2_verify_run.py poll  --out <dir> [--wait]
    python doc/7p2_verify_run.py serve-stop
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PORT = int(os.environ.get("PDF2ZH_BENCH_PORT", "4599"))
SERVE_URL = f"http://127.0.0.1:{PORT}"
MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
PID_FILE = Path(os.environ.get("TEMP", "/tmp")) / "oc_serve_7p2.pid"


def _hb(path: Path, payload: dict) -> None:
    """原子写：先 .tmp 再 replace，读者不会看到半个 JSON。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
def serve_start() -> int:
    exe = r"C:\Users\14977\AppData\Roaming\npm\opencode.ps1"
    if not Path(exe).is_file():
        print(f"opencode CLI not found at {exe}")
        return 2
    tmp = Path(os.environ.get("TEMP", "/tmp"))
    out_f = open(tmp / "oc_serve_7p2.out", "w", encoding="utf-8")
    err_f = open(tmp / "oc_serve_7p2.err", "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["pwsh", "-NoProfile", "-Command", f"& '{exe}' serve --port {PORT}"],
        stdout=out_f,
        stderr=err_f,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    out_f.close()
    err_f.close()

    import urllib.request

    # ``opencode serve --port N`` 会 spawn 一个**真正的 opencode.exe** 子进程，pwsh
    # 只是包装层。记包装 pid 会让 serve-stop 杀不掉真身（实测：包装 pid 活着、端口
    # 却无人监听，``taskkill /T`` 也只杀到包装），下一轮就撞上"端口已被占用"。
    # 所以健康检查通过后按端口反查真实 pid 并记它。
    real_pid = None
    for waited in range(90):
        time.sleep(1)
        if proc.poll() is not None:
            err = (tmp / "oc_serve_7p2.err").read_text(
                encoding="utf-8", errors="replace"
            )
            print(
                f"serve exited rc={proc.returncode} after {waited}s\nstderr: {err[:600]}"
            )
            return 1
        try:
            urllib.request.urlopen(f"{SERVE_URL}/global/health", timeout=3).read()
        except Exception:  # noqa: BLE001 -- 还没起来就继续等
            continue
        real_pid = _pid_listening_on(PORT)
        print(
            f"serve healthy on {SERVE_URL} after {waited + 1}s "
            f"(wrapper pid={proc.pid}, real pid={real_pid})"
        )
        break
    if real_pid is None:
        print(f"serve healthy but could not resolve the owning pid on port {PORT}")
        real_pid = proc.pid
    PID_FILE.write_text(str(real_pid), encoding="utf-8")
    return 0


def _pid_listening_on(port: int) -> int | None:
    """找出监听 ``port`` 的进程 pid。

    用 ``netstat -ano`` 而不是 psutil —— 本项目不依赖 psutil，一个对照实验脚本不该
    为了查 pid 引入新依赖。
    """
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:  # noqa: BLE001
        return None
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or not parts[1].endswith(f":{port}"):
            continue
        if parts[3] != "LISTENING":
            continue
        try:
            return int(parts[4])
        except ValueError:
            continue
    return None


def serve_stop() -> int:
    if not PID_FILE.is_file():
        print("no pid file")
        return 0
    pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    if pid:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=30
        )
        print(f"stopped serve pid {pid}")
    PID_FILE.unlink(missing_ok=True)
    return 0


# --------------------------------------------------------------------------
def launch(out_dir: Path, pages: str, thread: int, ignore_cache: bool = False) -> int:
    """后台起 7p0_real_load_run，并另起一个心跳线程盯着 run.log 的推进。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    hb_path = out_dir / "heartbeat.json"
    log = out_dir / "run.log"
    out_f = open(out_dir / "runner.out", "w", encoding="utf-8")
    err_f = open(out_dir / "runner.err", "w", encoding="utf-8")
    env = dict(os.environ)
    env["PDF2ZH_BENCH_SERVER"] = SERVE_URL
    proc = subprocess.Popen(
        [
            sys.executable,
            "-X",
            "utf8",
            str(ROOT / "doc" / "7p0_real_load_run.py"),
            "--out",
            str(out_dir),
            "--pages",
            pages,
            "--thread",
            str(thread),
        ]
        + (["--ignore-cache"] if ignore_cache else []),
        cwd=str(ROOT),
        stdout=out_f,
        stderr=err_f,
        env=env,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
    )
    out_f.close()
    err_f.close()
    (out_dir / "runner.pid").write_text(str(proc.pid), encoding="utf-8")

    # 心跳必须**立刻**先写一份：否则调用方在前 10s 只能看到 "no heartbeat yet"，
    # 分不清"刚启动"和"没起来"（实测踩过）。
    _hb(
        hb_path,
        {
            "phase": "starting",
            "log_bytes": 0,
            "idle_s": 0,
            "stalled": False,
            "elapsed_s": 0,
            "pid": proc.pid,
        },
    )

    import threading

    t0 = time.time()
    state = {"last_log": "", "last_change": time.time(), "phase": "starting"}

    def _watch():
        while True:
            time.sleep(10)
            if proc.poll() is not None:
                _hb(
                    hb_path,
                    {
                        "phase": "done",
                        "exit": proc.returncode,
                        "elapsed_s": round(time.time() - t0, 1),
                        "pid": proc.pid,
                    },
                )
                return
            try:
                txt = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                txt = ""
            if txt != state["last_log"]:
                state["last_log"] = txt
                state["last_change"] = time.time()
            # 从日志里粗判阶段，供人看
            phase = "parse" if "[magicpdf] page slice" in txt else state["phase"]
            if "translating blocks" in txt:
                phase = "translate"
            if "parse dump" in txt:
                phase = "render"
            state["phase"] = phase
            idle = round(time.time() - state["last_change"], 1)
            _hb(
                hb_path,
                {
                    "phase": phase,
                    "log_bytes": len(txt),
                    "idle_s": idle,
                    "stalled": idle > 300,
                    "elapsed_s": round(time.time() - t0, 1),
                    "pid": proc.pid,
                },
            )

    threading.Thread(target=_watch, daemon=True).start()
    print(f"runner pid={proc.pid}  out={out_dir}")
    print(f"heartbeat -> {hb_path}")
    return 0


def _pid_alive(pid: int) -> bool:
    """进程是否存活。

    ``os.kill(pid, 0)`` 在 Windows 上抛 ``SystemError: [WinError 87]``（把 0 当成
    信号量），不能用。用 ``tasklist`` 过滤，既准确又不碰任何信号语义。
    """
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=20,
        )
        return str(pid) in out.stdout
    except Exception:  # noqa: BLE001 -- 查不到就当它不在
        return False


def poll(out_dir: Path, wait: bool, every: int) -> int:
    hb = out_dir / "heartbeat.json"
    pid_f = out_dir / "runner.pid"
    deadline = time.time() + (3600 if wait else 0)
    while True:
        alive = False
        pid = None
        if pid_f.is_file():
            pid = int(pid_f.read_text(encoding="utf-8").strip() or 0)
            if pid:
                # Windows 上 os.kill(pid, 0) 会抛 SystemError（WinError 87）而不是
                # 返回——它把 0 当成真实信号量了。改用 tasklist 查，跨平台地稳。
                alive = _pid_alive(pid)
        if hb.is_file():
            d = json.loads(hb.read_text(encoding="utf-8"))
            print(
                f"phase={d.get('phase'):<9} elapsed={d.get('elapsed_s', 0):>7.0f}s "
                f"idle={d.get('idle_s', '-'):>6} stalled={d.get('stalled', False)} "
                f"alive={alive}"
            )
            if d.get("phase") == "done":
                print(f"finished: exit={d.get('exit')} elapsed={d.get('elapsed_s')}s")
                return 0
        else:
            print(f"no heartbeat yet (alive={alive})")
        if not wait or time.time() > deadline:
            return 0 if alive or hb.is_file() else 1
        time.sleep(every)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve-start")
    sub.add_parser("serve-stop")
    la = sub.add_parser("launch")
    la.add_argument("--out", required=True)
    la.add_argument("--pages", default="1-50")
    la.add_argument("--thread", type=int, default=4)
    la.add_argument(
        "--ignore-cache",
        action="store_true",
        help="绕过翻译缓存。真机吞吐对照必需，否则测到的是缓存时间。",
    )
    po = sub.add_parser("poll")
    po.add_argument("--out", required=True)
    po.add_argument("--wait", action="store_true")
    po.add_argument("--every", type=int, default=30)
    args = ap.parse_args()

    if args.cmd == "serve-start":
        return serve_start()
    if args.cmd == "serve-stop":
        return serve_stop()
    if args.cmd == "launch":
        return launch(
            Path(args.out), args.pages, args.thread, ignore_cache=args.ignore_cache
        )
    return poll(Path(args.out), args.wait, args.every)


if __name__ == "__main__":
    raise SystemExit(main())
