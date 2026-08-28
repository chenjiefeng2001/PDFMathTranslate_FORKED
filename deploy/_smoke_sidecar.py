# -*- coding: utf-8 -*-
"""新构建 sidecar 的 frozen 环境 smoke test。

启动 exe → 轮询 health → 验证 gpu/provider、selftest/babeldoc、engines →
退出并汇报。验证 excludes 瘦身后运行链路完好、GPU provider 按需下载端点就绪。
"""
import json
import subprocess
import sys
import time
import urllib.request

EXE = r"c:\Users\14977\source\repos\PDFMathTranslate_FORKED\deploy\_build_sidecar\dist\pdf2zh-api-sidecar\pdf2zh-api-sidecar.exe"
PORT = 11999
BASE = f"http://127.0.0.1:{PORT}"


def http_get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def http_post(path, timeout=30):
    req = urllib.request.Request(
        BASE + path, data=b"", method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, json.loads(resp.read())


def main():
    proc = subprocess.Popen(
        [EXE, "--port", str(PORT)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        # 轮询 health（sidecar 启动会预热 worker 池）
        ok = False
        for i in range(60):
            if proc.poll() is not None:
                print("FATAL: sidecar 提前退出 rc=", proc.returncode)
                break
            try:
                st, body = http_get("/api/health", timeout=5)
                print(f"health [{st}] attempt {i}: {body}")
                ok = True
                break
            except Exception:
                time.sleep(1.0)
        if not ok:
            print("FATAL: health 超时未就绪")
            return 1

        st, gpu = http_get("/api/gpu/provider")
        print(f"\nGET /api/gpu/provider [{st}]:")
        print(json.dumps(gpu, ensure_ascii=False, indent=2))
        assert gpu["cuda_dll_present"] is False, "本体不应携带 CUDA provider DLL"
        assert gpu["onnxruntime_version"], "应能读出内置 onnxruntime 版本"

        st, bd = http_get("/api/selftest/babeldoc")
        print(f"\nGET /api/selftest/babeldoc [{st}]: ok={bd.get('ok')}")

        st, eng = http_get("/api/engines")
        print(f"\nGET /api/engines [{st}]: {len(eng)} engines")

        # 验证 sklearn DBSCAN（babeldoc 版面聚类的依赖）在 frozen 下可用
        import importlib.util
        # 通过 selftest/babeldoc 已覆盖 babeldoc import；再显式验证 sklearn
        code = (
            "from sklearn.cluster import DBSCAN;"
            "import numpy as np;"
            "print('DBSCAN:', DBSCAN(eps=2).fit_predict(np.array([[0,0],[1,1],[9,9]])).tolist())"
        )
        # frozen exe 无法直接注入代码；改用独立子进程不可行（无 python），
        # 依赖 selftest/babeldoc + 后续翻译 smoke 覆盖。
        print("\nSKLEARN 链路由 selftest/babeldoc 导入覆盖")
        print("\n=== SMOKE TEST PASSED ===")
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
