"""babeldoc 子进程 runner 的测试 stub worker（tests 专用，极轻量）。

由 ``tests/test_perf_optimizations.py`` 经
``PDF2ZH_BABELDOC_WORKER_MODULE=tests.stub_babeldoc_worker`` 注入，
避免真实 pdf2zh_next 内核的重量级导入。协议与
``pdf2zh.babeldoc_next_worker`` 一致（NDJSON）。

行为约定（按 payload.source_path 文件名路由）：
- ``ok.pdf``    → 进度帧 + 成功帧（固定 files）
- ``boom.pdf``  → 失败帧（FileNotFoundError）
- ``unavail.pdf`` → 失败帧（error_type=BabeldocNextUnavailableError）
- ``slow.pdf``  → 长眠（供父进程取消/kill 测试）
"""

import json
import sys
import time


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"ok": False, "error": "bad payload", "error_type": "ValueError"}))
        return 1
    name = str(payload.get("source_path") or "")
    if name.endswith("boom.pdf"):
        print(json.dumps({"ok": False, "error": "FileNotFoundError: boom", "error_type": "FileNotFoundError"}))
        return 1
    if name.endswith("unavail.pdf"):
        print(json.dumps({"ok": False, "error": "kernel missing", "error_type": "BabeldocNextUnavailableError"}))
        return 2
    if name.endswith("slow.pdf"):
        time.sleep(120)
        print(json.dumps({"ok": False, "error": "killed", "error_type": "KilledError"}))
        return 1
    print(json.dumps({"progress": True, "stage": "translating", "pct": 50.0, "msg": "half"}))
    print(json.dumps({"ok": True, "files": [{"name": "a_mono.pdf", "path": "x/a_mono.pdf"}]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
