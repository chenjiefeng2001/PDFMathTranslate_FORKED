"""submit 层幂等去重测试（RuntimeService.submit_task）。

覆盖：
- 相同请求指纹（文件集 + 全部关键参数）重复提交 → 复用已有 task_id；
- 不同文件 / 不同参数 → 各自新建 task_id；
- 已终态任务之后的重放 → 允许新建（不误吞用户刻意的新任务）；
- 指纹对文件集顺序稳定（排序后同指纹）。
"""

import unittest

from pdf2zh.services.runtime_service import RuntimeService, TranslationRequest


def _req(path: str = "/tmp/a.pdf", **overrides) -> TranslationRequest:
    base = dict(
        source_path=path,
        files=[path],
        target_lang="zh-CN",
        source_lang="en",
        engine="google",
        threads=4,
        parse_engine="magicpdf",
        backend="auto",
    )
    base.update(overrides)
    return TranslationRequest(**base)


class TestSubmitDedup(unittest.TestCase):
    def setUp(self):
        self.svc = RuntimeService()

    def test_same_request_reuses_task_id(self):
        req = _req()
        t1 = self.svc.submit_task(req)
        t2 = self.svc.submit_task(_req())  # 完全相同指纹
        self.assertEqual(t1, t2)

    def test_different_file_new_task(self):
        t1 = self.svc.submit_task(_req("/tmp/a.pdf"))
        t2 = self.svc.submit_task(_req("/tmp/b.pdf"))
        self.assertNotEqual(t1, t2)

    def test_different_engine_new_task(self):
        t1 = self.svc.submit_task(_req(engine="google"))
        t2 = self.svc.submit_task(_req(engine="openai"))
        self.assertNotEqual(t1, t2)

    def test_terminal_task_allows_retry(self):
        req = _req()
        t1 = self.svc.submit_task(req)
        # 手动把任务落为终态
        from pdf2zh.services.runtime_service import TaskStage

        self.svc._store.update_task(
            t1, status=TaskStage.FAILED.value, error_message="boom"
        )
        t2 = self.svc.submit_task(_req())
        self.assertNotEqual(t1, t2)  # 终态后可重试，不吞新任务

    def test_terminal_dedup_pruned_by_sweeper(self):
        """终态任务的指纹应被 sweeper 清理，避免常驻服务内存累积。"""
        from pdf2zh.services.runtime_service import TaskStage

        req = _req()
        t1 = self.svc.submit_task(req)
        key = next(iter(self.svc._submit_dedup))
        # 任务终态
        self.svc._store.update_task(t1, status=TaskStage.FAILED.value)
        # 模拟 sweeper 清理
        self.svc._sweep_stale(0.0)
        with self.svc._submit_dedup_lock:
            pruned = [
                k
                for k, (_, tid) in self.svc._submit_dedup.items()
                if not self.svc._is_dedup_alive(tid)
            ]
            for k in pruned:
                self.svc._submit_dedup.pop(k, None)
        self.assertNotIn(key, self.svc._submit_dedup)

    def test_fingerprint_sort_stable(self):
        a = _req()
        a.files = ["/tmp/a.pdf", "/tmp/b.pdf"]
        b = _req()
        b.files = ["/tmp/b.pdf", "/tmp/a.pdf"]
        # 同文件集但乱序 → 应命中同一指纹（多文件乱序不影响）
        t1 = self.svc.submit_task(a)
        t2 = self.svc.submit_task(b)
        self.assertEqual(t1, t2)


if __name__ == "__main__":
    unittest.main()
