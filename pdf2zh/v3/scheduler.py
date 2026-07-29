"""Module: Execution Runtime — V4.1 TaskGraph + Scheduler + Executor.

Provides a task-based execution framework for the V4 architecture.

Usage::
    from pdf2zh.v3.scheduler import TaskGraph, Task, TaskStatus, Executor

    tg = TaskGraph()
    task_a = Task("parse_doc", "Parse", module="parser", priority=10)
    task_b = Task("analyze", "Analyze", module="analyzer", priority=20)
    task_b.depends_on(task_a.id)
    tg.add_task(task_a)
    tg.add_task(task_b)

    executor = Executor(tg)
    results = executor.run_all()
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class TaskStatus(Enum):
    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    RETRY = "retry"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class Task:
    """A single unit of work in the Execution Runtime."""
    id: str
    name: str
    module: str = ""
    description: str = ""
    priority: int = 50
    status: TaskStatus = TaskStatus.PENDING
    dependencies: Set[str] = field(default_factory=set)
    handler: Optional[Callable] = None
    result: Any = None
    error: Optional[str] = None
    retry_count: int = 0
    max_retries: int = 2
    metadata: dict = field(default_factory=dict)

    def depends_on(self, task_id: str) -> None:
        self.dependencies.add(task_id)

    @property
    def is_ready(self) -> bool:
        return self.status in (TaskStatus.PENDING, TaskStatus.RETRY)

    @property
    def is_terminal(self) -> bool:
        return self.status in (TaskStatus.DONE, TaskStatus.FAILED,
                               TaskStatus.SKIPPED)

    @property
    def can_retry(self) -> bool:
        return self.retry_count < self.max_retries


class TaskGraph:
    """A DAG of Tasks with dependency resolution."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Task] = {}
        self._name_index: Dict[str, str] = {}

    def add_task(self, task: Task) -> "TaskGraph":
        if task.id in self._tasks:
            raise ValueError(f"Task '{task.id}' already exists")
        self._tasks[task.id] = task
        self._name_index[task.name] = task.id
        return self

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def get_task_by_name(self, name: str) -> Optional[Task]:
        tid = self._name_index.get(name)
        return self._tasks.get(tid) if tid else None

    def remove_task(self, task_id: str) -> None:
        task = self._tasks.pop(task_id, None)
        if task:
            self._name_index.pop(task.name, None)
            for t in self._tasks.values():

                t.dependencies.discard(task_id)

    def get_ready_tasks(self) -> List[Task]:
        ready = []
        for task in self._tasks.values():
            if not task.is_ready:
                continue
            if all(
                self._tasks.get(dep, Task("", "")).status == TaskStatus.DONE
                for dep in task.dependencies
            ):
                ready.append(task)
        return ready

    def get_dependents(self, task_id: str) -> List[Task]:
        return [
            t for t in self._tasks.values()
            if task_id in t.dependencies
        ]

    def clear(self) -> None:
        """Remove all tasks from the graph."""
        self._tasks.clear()
        self._name_index.clear()

    @property
    def tasks(self) -> List[Task]:
        return list(self._tasks.values())

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def pending_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.is_ready)

    @property
    def done_count(self) -> int:
        return sum(1 for t in self._tasks.values()
                   if t.status == TaskStatus.DONE)

    @property
    def failed_count(self) -> int:
        return sum(1 for t in self._tasks.values()
                   if t.status == TaskStatus.FAILED)

    @property
    def is_complete(self) -> bool:
        return self.done_count + self.failed_count == len(self._tasks)

    def topological_sort(self) -> List[Task]:
        """Kahn's algorithm with priority ordering."""
        in_degree: Dict[str, int] = {}
        for task in self._tasks.values():
            in_degree[task.id] = len(task.dependencies)
        queue = [t for t in self._tasks.values()
                 if in_degree[t.id] == 0]
        result = []
        while queue:
            queue.sort(key=lambda t: t.priority)
            task = queue.pop(0)
            result.append(task)
            for dep in self.get_dependents(task.id):
                in_degree[dep.id] -= 1
                if in_degree[dep.id] == 0:
                    queue.append(dep)
        return result

class Executor:
    """Executes tasks from a TaskGraph with retry support.

    Usage::
        executor = Executor(task_graph)
        results = executor.run_all()
    """

    def __init__(
        self,
        graph: TaskGraph,
        *,
        parallel: bool = False,
    ):
        self._graph = graph
        self._parallel = parallel
        self._results: List[Tuple[str, Task]] = []

    @property
    def results(self) -> List[Tuple[str, Task]]:
        return list(self._results)

    def run_all(self) -> List[Tuple[str, Task]]:
        ordered = self._graph.topological_sort()
        final_results: List[Tuple[str, Task]] = []
        for task in ordered:
            final_results.append(self._execute_task(task))
        self._results = final_results
        return final_results

    def run_ready(self) -> List[Tuple[str, Task]]:
        ready = self._graph.get_ready_tasks()
        return [self._execute_task(t) for t in ready]

    def _execute_task(self, task: Task) -> Tuple[str, Task]:
        task.status = TaskStatus.RUNNING
        if task.handler is None:
            task.status = TaskStatus.DONE
            return (task.id, task)
        for attempt in range(task.max_retries + 1):
            try:
                task.retry_count = attempt
                result = task.handler(task)
                task.result = result
                task.status = TaskStatus.DONE
                return (task.id, task)
            except Exception as e:
                task.error = str(e)
                if attempt < task.max_retries:
                    task.status = TaskStatus.RETRY
                else:
                    task.status = TaskStatus.FAILED
        return (task.id, task)

    def run_selective(self, task_ids: Set[str]) -> List[Tuple[str, Task]]:
        needed = set(task_ids)
        for tid in task_ids:
            task = self._graph.get_task(tid)
            if task:
                for dep_id in task.dependencies:
                    dep = self._graph.get_task(dep_id)
                    if dep and dep.status != TaskStatus.DONE:
                        needed.add(dep_id)
        results = []
        ordered = self._graph.topological_sort()
        for task in ordered:
            if task.id in needed and not task.is_terminal:
                results.append(self._execute_task(task))
        return results


class Scheduler:
    """Orchestrates TaskGraph creation, execution, and lifecycle."""

    def __init__(self) -> None:
        self._graph = TaskGraph()
        self._executor: Optional[Executor] = None

    @property
    def graph(self) -> TaskGraph:
        return self._graph

    def create_task(
        self, task_id: str, name: str, *,
        module: str = "", handler: Optional[Callable] = None,
        priority: int = 50,
        dependencies: Optional[List[str]] = None,
        max_retries: int = 2,
    ) -> Task:
        task = Task(
            id=task_id, name=name, module=module,
            priority=priority, handler=handler,
            max_retries=max_retries,
        )
        if dependencies:
            for dep in dependencies:
                task.depends_on(dep)
        self._graph.add_task(task)
        return task

    def run(self, parallel: bool = False) -> List[Tuple[str, Task]]:
        self._executor = Executor(self._graph, parallel=parallel)
        return self._executor.run_all()

    def run_selective(self, task_ids: Set[str]) -> List[Tuple[str, Task]]:
        self._executor = Executor(self._graph)
        return self._executor.run_selective(task_ids)

    def get_stats(self) -> dict:
        return {
            "total": self._graph.task_count,
            "done": self._graph.done_count,
            "failed": self._graph.failed_count,
            "pending": self._graph.pending_count,
        }


__all__ = [
    "Task", "TaskStatus", "TaskGraph",
    "Executor", "Scheduler",
]
