"""并发基准：翻译阶段在 1/4/8 线程下的真实加速（心跳版，不阻塞调用方）。

为什么要有心跳
--------------
上一轮把 ``opencode serve`` 用 ``Start-Process -RedirectStandard*`` 起在同一条命令里，
结果常驻进程一直持有控制台句柄，调用方看到的是"挂起"，而基准其实早已打印完。
这次把三件事彻底拆开：

1. serve 单独起、**不带任何重定向**（由 :func:`start_serve` 负责记录 pid）；
2. 基准跑在**独立后台进程**里，逐段写 JSONL 心跳文件；
3. 调用方只轮询心跳文件，因此无论基准跑 30 秒还是 30 分钟都不会被挂住。

心跳里带 ``phase`` 与 ``done_calls``，所以「还在跑」和「卡住了」能区分开：
若 ``done_calls`` 长时间不变而 ``elapsed`` 在涨，才是真的停滞。

用法::

    python doc/7p1_concurrency_bench.py serve-start
    python doc/7p1_concurrency_bench.py run --out <dir> --threads 1,4,8
    python doc/7p1_concurrency_bench.py serve-stop
    python doc/7p1_concurrency_bench.py status --out <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
PORT = 4602
SERVE_URL = f"http://127.0.0.1:{PORT}"

#: 8 段互不相同的文本（缓存不命中，否则测的是本地查表）。
TEXTS = [
    "A mutex is a lock only one thread may hold at a time.",
    "Lock-freedom is weaker than wait-freedom in the progress it guarantees.",
    "The bakery algorithm assigns each process a numbered ticket and waits its turn.",
    "Linearizability means each operation appears to take effect instantaneously.",
    "Amdahl's law bounds the speedup by the parallelisable fraction of the work.",
    "Sequential consistency is the strongest practical memory model in this model.",
    "A data race is undefined behaviour rather than merely an ordinary bug.",
    "Cache coherence protocols keep the per-core views of memory consistent.",
]


def _hb(path: Path, payload: dict) -> None:
    """原子写心跳：先写 .tmp 再 replace，读者永远看不到半个 JSON。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# serve 生命周期（与基准进程分离）
# --------------------------------------------------------------------------
def start_serve() -> int:
    """后台起 opencode serve，输出**写文件**而不是继承调用方句柄。

    两种失败都实测过：
    - ``DETACHED_PROCESS`` + 不重定向：进程直接消失，端口从不监听（Windows 上
      detached 的 pwsh 拿不到 npm shim 的环境）。所以输出必须落到文件。
    - 继承/重定向到管道：serve 永久持有该句柄，调用方的 shell 永远等不到 EOF ——
      这正是上一轮被误判成"挂起"的原因。

    写文件同时解决两者：进程有自己的落点，不碰调用方任何句柄。
    """
    exe = r"C:\Users\14977\AppData\Roaming\npm\opencode.ps1"
    if not Path(exe).is_file():
        print(f"opencode CLI not found at {exe}")
        return 2
    tmp = Path(os.environ.get("TEMP", "/tmp"))
    out_f = open(tmp / "oc_serve_7p1.out", "w", encoding="utf-8")
    err_f = open(tmp / "oc_serve_7p1.err", "w", encoding="utf-8")
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        ["pwsh", "-NoProfile", "-Command", f"& '{exe}' serve --port {PORT}"],
        stdout=out_f,
        stderr=err_f,
        stdin=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )
    out_f.close()
    err_f.close()
    (tmp / "oc_serve_7p1.pid").write_text(str(proc.pid), encoding="utf-8")

    import urllib.request

    for waited in range(90):
        time.sleep(1)
        if proc.poll() is not None:
            err = (tmp / "oc_serve_7p1.err").read_text(
                encoding="utf-8", errors="replace"
            )
            print(f"serve exited rc={proc.returncode} after {waited}s")
            print("stderr:", err[:600] or "(empty)")
            return 1
        try:
            urllib.request.urlopen(f"{SERVE_URL}/global/health", timeout=3).read()
            print(f"serve healthy on {SERVE_URL} (pid {proc.pid}) after {waited + 1}s")
            return 0
        except Exception:  # noqa: BLE001 -- 还没起来就继续等
            continue
    print(f"serve not healthy within 90s (pid {proc.pid})")
    print(
        "stderr:",
        (tmp / "oc_serve_7p1.err").read_text(encoding="utf-8", errors="replace")[:600],
    )
    return 1


def stop_serve() -> int:
    pid_file = Path(os.environ.get("TEMP", "/tmp")) / "oc_serve_7p1.pid"
    if not pid_file.is_file():
        print("no pid file; nothing to stop")
        return 0
    pid = int(pid_file.read_text(encoding="utf-8").strip() or 0)
    if not pid:
        return 0
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=30,
        )
        print(f"stopped serve pid {pid}")
    except Exception as exc:  # noqa: BLE001
        print(f"stop failed: {exc}")
    pid_file.unlink(missing_ok=True)
    return 0


# --------------------------------------------------------------------------
# 基准
# --------------------------------------------------------------------------
def run_bench(out_dir: Path, thread_counts: list[int]) -> int:
    from pdf2zh.networking import import_system_proxy_to_env, sanitize_loopback_proxy

    import_system_proxy_to_env()
    sanitize_loopback_proxy()
    os.environ.update(
        {
            "OPENCODE_SERVER_URL": SERVE_URL,
            "OPENCODE_MODEL": MODEL,
            "OPENCODE_TIMEOUT": "180",
            "OPENCODE_AGENT": "build",
        }
    )
    from pdf2zh.translator import OpenCodeTranslator

    hb_path = out_dir / "heartbeat.json"
    done = {"n": 0}
    lock = threading.Lock()
    t_start = time.time()

    # 独立心跳线程：即使翻译调用本身卡住，调用方也能看到 elapsed 在涨。
    stop_evt = threading.Event()

    def _pulse():
        while not stop_evt.wait(5.0):
            with lock:
                n = done["n"]
            _hb(
                hb_path,
                {
                    "phase": "running",
                    "done_calls": n,
                    "total_calls": len(TEXTS) * len(thread_counts),
                    "elapsed_s": round(time.time() - t_start, 1),
                    "pid": os.getpid(),
                },
            )

    pulse = threading.Thread(target=_pulse, daemon=True)
    pulse.start()

    tr = OpenCodeTranslator("en", "zh", MODEL, ignore_cache=True)
    results = []
    for threads in thread_counts:
        _hb(
            hb_path,
            {
                "phase": f"threads={threads}",
                "done_calls": 0,
                "elapsed_s": round(time.time() - t_start, 1),
                "pid": os.getpid(),
            },
        )

        def _one(text: str) -> str:
            out = tr.translate(text)
            with lock:
                done["n"] += 1
                _hb(
                    hb_path,
                    {
                        "phase": f"threads={threads}",
                        "done_calls": done["n"],
                        "elapsed_s": round(time.time() - t_start, 1),
                        "pid": os.getpid(),
                    },
                )
            return out or ""

        import concurrent.futures as cf

        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=threads) as ex:
            outs = list(ex.map(_one, TEXTS))
        el = time.time() - t0
        ok = sum(1 for o in outs if o.strip())
        results.append(
            {
                "threads": threads,
                "elapsed_s": round(el, 1),
                "per_call_s": round(el / len(TEXTS), 2),
                "ok": ok,
                "samples": [o[:60] for o in outs[:3]],
            }
        )
        print(
            f"threads={threads}: {el:.1f}s for {len(TEXTS)} calls "
            f"({el/len(TEXTS):.2f}s/call) ok={ok}",
            flush=True,
        )

    stop_evt.set()
    base = next((r["per_call_s"] for r in results if r["threads"] == 1), None)
    for r in results:
        r["speedup_vs_1"] = round(base / r["per_call_s"], 2) if base else None
    payload = {"model": MODEL, "texts": len(TEXTS), "results": results}
    (out_dir / "bench.json").write_text(
        json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    _hb(
        hb_path,
        {
            "phase": "done",
            "done_calls": done["n"],
            "total_calls": done["n"],
            "elapsed_s": round(time.time() - t_start, 1),
            "pid": os.getpid(),
        },
    )
    print(json.dumps(payload, indent=1, ensure_ascii=False))
    return 0


def status(out_dir: Path) -> int:
    hb = out_dir / "heartbeat.json"
    if not hb.is_file():
        print("no heartbeat yet")
        return 1
    d = json.loads(hb.read_text(encoding="utf-8"))
    age = time.time() - hb.stat().st_mtime
    print(json.dumps(d, ensure_ascii=False))
    print(f"heartbeat age: {age:.1f}s  ->  {'STALE (hung?)' if age > 60 else 'fresh'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve-start")
    sub.add_parser("serve-stop")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--threads", default="1,4,8")
    s = sub.add_parser("status")
    s.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.cmd == "serve-start":
        return start_serve()
    if args.cmd == "serve-stop":
        return stop_serve()
    if args.cmd == "status":
        return status(Path(args.out))
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    return run_bench(out_dir, [int(x) for x in args.threads.split(",") if x.strip()])


if __name__ == "__main__":
    raise SystemExit(main())
