"""真实翻译负载测量 —— 当前 HEAD，mp2e 前 50 页，NVIDIA Nemotron 3 Ultra (free) via OpenRouter。

与 doc/7n_real_mp2e.py 的区别：那套 harness 的产物只做 MECH-1/MECH-2 对齐，
而 doc/7n9-mp2e-fix3 的 qualification 是**空的**（562 页 0 页评分、
11467 事件 0 条规则、rules: []），所以本脚本自带度量，不依赖那套审计。

翻译服务：opencode 常驻服务 -> openrouter/nvidia/nemotron-3-ultra-550b-a55b:free
  - 直接用 OPENCODE_API_KEY 打 OpenRouter 是 401（那是 opencode 网关的 key，
    不是 sk-or-v1-），所以走 opencode 网关的 openrouter provider。
  - 常驻 `opencode serve` 是必须的：CLI 冷启动实测 16-21s/次，常驻后 ~9.7s/次。

用法:
    python doc/7p0_real_load_run.py --out <dir> --pages 1-50
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BOOK = ROOT / "tests" / "file" / "The Art of Multiprocessor Programming, 2e.pdf"
MODEL = "openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
SERVER = "http://127.0.0.1:4599"


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=20,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--pages", default="1-50")
    ap.add_argument("--thread", type=int, default=4)
    ap.add_argument("--service", default="opencode")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "output").mkdir(exist_ok=True)

    # 翻译服务环境必须在 translator registry 构建**之前**就位：set_envs 只在
    # 构造时从 os.environ 拷一次（这也是 d2906ef 那条 sidecar 顺序约束）。
    os.environ["OPENCODE_SERVER_URL"] = SERVER
    os.environ["OPENCODE_MODEL"] = MODEL
    os.environ["OPENCODE_TIMEOUT"] = "180"
    os.environ["OPENCODE_AGENT"] = "build"
    # 生产式环境：关自动切引擎、串行跑页
    os.environ["PDF2ZH_AUTO_SWITCH_MAGICPDF"] = "0"
    os.environ.setdefault("PDF2ZH_NO_PARALLEL", "1")

    argv = [
        str(BOOK),
        "--parse-engine",
        "magicpdf",
        "--lang-in",
        "en",
        "--lang-out",
        "zh",
        "--service",
        args.service,
        "--output",
        str(out_dir / "output"),
        "--pages",
        args.pages,
        "--no-parallel",
        "--thread",
        str(args.thread),
        "--magicpdf-ocr-mode",
        "off",
    ]

    env_record = {
        "git_head": _git_head(),
        "git_dirty": _git_dirty(),
        "book": BOOK.name,
        "book_pages": 562,
        "pages": args.pages,
        "thread": args.thread,
        "service": args.service,
        "model": MODEL,
        "server": SERVER,
        "python": sys.version.replace("\n", " "),
        "argv": argv,
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "run-config.json").write_text(
        json.dumps(env_record, indent=1, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(env_record, indent=1, ensure_ascii=False))

    log_fh = open(out_dir / "run.log", "w", encoding="utf-8")

    class _H(logging.Handler):
        def emit(self, record):
            try:
                log_fh.write(self.format(record) + "\n")
                log_fh.flush()
            except Exception:  # noqa: BLE001
                pass

    handler = _H()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s")
    )
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)

    import pdf2zh.pdf2zh as cli

    stdout_buf = io.StringIO()
    t0 = time.time()
    code = 1
    try:
        with redirect_stdout(stdout_buf):
            code = cli.main(argv)
    except SystemExit as e:  # argparse
        code = int(e.code or 0)
    except Exception as exc:  # noqa: BLE001
        log_fh.write(f"\nHARNESS EXCEPTION: {type(exc).__name__}: {exc}\n")
        log_fh.flush()
        import traceback

        log_fh.write(traceback.format_exc())
        code = 3
    finally:
        elapsed = time.time() - t0
        log_fh.write(f"\n=== exit={code} elapsed={elapsed:.1f}s ===\n")
        log_fh.write(stdout_buf.getvalue())
        root.removeHandler(handler)
        log_fh.close()

    summary = {
        "exit": code,
        "elapsed_s": round(elapsed, 1),
        "pages": args.pages,
        "model": MODEL,
    }
    (out_dir / "run-summary.json").write_text(
        json.dumps(summary, indent=1), encoding="utf-8"
    )
    print(json.dumps(summary, indent=1))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
