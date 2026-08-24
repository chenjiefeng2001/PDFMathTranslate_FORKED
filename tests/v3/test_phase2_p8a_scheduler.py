import os, sys, unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    from pdf2zh.v3.scheduler import TaskGraph, Task, TaskStatus, Executor

    _HAS = True
except ImportError as e:
    _HAS = False
    print(f"Scheduler import error: {e}")


@unittest.skipIf(not _HAS, "not importable")
class TestTask(unittest.TestCase):
    def test_creation(self):
        t = Task("t1", "Test")
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.name, "Test")
        self.assertEqual(t.status, TaskStatus.PENDING)

    def test_with_module(self):
        t = Task("t1", "Test", module="parser")
        self.assertEqual(t.module, "parser")

    def test_is_ready_pending(self):
        self.assertTrue(Task("t1", "Test").is_ready)

    def test_is_ready_running(self):
        t = Task("t1", "Test", status=TaskStatus.RUNNING)
        self.assertFalse(t.is_ready)

    def test_is_ready_done(self):
        t = Task("t1", "Test", status=TaskStatus.DONE)
        self.assertFalse(t.is_ready)

    def test_depends_on(self):
        t = Task("t1", "Test")
        t.depends_on("dep1")
        self.assertIn("dep1", t.dependencies)

    def test_priority(self):
        a = Task("a", "A", priority=10)
        b = Task("b", "B", priority=20)
        self.assertLess(a.priority, b.priority)

    def test_max_retries(self):
        self.assertEqual(Task("t1", "Test").max_retries, 2)

    def test_metadata(self):
        t = Task("t1", "Test", metadata={"lang": "en"})
        self.assertEqual(t.metadata["lang"], "en")

    def test_handler(self):
        t = Task("t1", "Test", handler=lambda: "done")
        self.assertIsNotNone(t.handler)

    def test_error_default(self):
        t = Task("t1", "Test")
        self.assertIsNone(t.error)

    def test_is_terminal(self):
        self.assertTrue(Task("t1", "T", status=TaskStatus.DONE).is_terminal)
        self.assertFalse(Task("t1", "T", status=TaskStatus.RUNNING).is_terminal)

    def test_can_retry(self):
        t = Task("t1", "T", max_retries=3)
        self.assertTrue(t.can_retry)
        t.retry_count = 3
        self.assertFalse(t.can_retry)


@unittest.skipIf(not _HAS, "not importable")
class TestTaskGraph(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(TaskGraph().task_count, 0)

    def test_add_task(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "Test"))
        self.assertEqual(tg.task_count, 1)

    def test_add_duplicate_raises(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        with self.assertRaises(ValueError):
            tg.add_task(Task("t1", "T1_dup"))

    def test_get_ready(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        tg.add_task(Task("t2", "T2", dependencies={"t1"}))
        self.assertEqual(len(tg.get_ready_tasks()), 1)
        self.assertEqual(tg.get_ready_tasks()[0].id, "t1")

    def test_is_complete(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        self.assertFalse(tg.is_complete)

    def test_done_count(self):
        self.assertEqual(TaskGraph().done_count, 0)

    def test_failed_count(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1", status=TaskStatus.FAILED))
        self.assertEqual(tg.failed_count, 1)

    def test_topological_sort_empty(self):
        self.assertEqual(TaskGraph().topological_sort(), [])

    def test_topological_sort(self):
        tg = TaskGraph()
        tg.add_task(Task("a", "A", priority=10))
        tg.add_task(Task("b", "B", priority=20, dependencies={"a"}))
        self.assertEqual([t.id for t in tg.topological_sort()], ["a", "b"])

    def test_remove_task(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        tg.remove_task("t1")
        self.assertEqual(tg.task_count, 0)

    def test_clear(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        tg.clear()
        self.assertEqual(tg.task_count, 0)

    def test_get_dependents(self):
        tg = TaskGraph()
        tg.add_task(Task("a", "A"))
        tg.add_task(Task("b", "B", dependencies={"a"}))
        self.assertEqual(len(tg.get_dependents("a")), 1)

    def test_task_property(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        self.assertEqual(len(tg.tasks), 1)

    def test_pending_count(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "T1"))
        tg.add_task(Task("t2", "T2", dependencies={"t1"}))
        self.assertEqual(tg.pending_count, 2)

    def test_get_task_by_name(self):
        tg = TaskGraph()
        tg.add_task(Task("t1", "MyTask"))
        self.assertIsNotNone(tg.get_task_by_name("MyTask"))

    def test_get_task_by_name_missing(self):
        self.assertIsNone(TaskGraph().get_task_by_name("nope"))


@unittest.skipIf(not _HAS, "not importable")
class TestExecutor(unittest.TestCase):
    def test_init(self):
        self.assertIsNotNone(Executor(TaskGraph()))

    def test_run_all_empty(self):
        self.assertEqual(Executor(TaskGraph()).run_all(), [])

    def test_run_ready_empty(self):
        self.assertEqual(Executor(TaskGraph()).run_ready(), [])

    def test_results(self):
        self.assertEqual(Executor(TaskGraph()).results, [])

    def test_parallel(self):
        self.assertIsNotNone(Executor(TaskGraph(), parallel=True))
