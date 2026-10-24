"""One event loop for the whole backend, and the tasks that run on it.

Every service is async and lives on the single backend loop (its own
thread).  Callers on other threads (the WebView bridge) use
:meth:`Runtime.call` for quick requests and :class:`TaskManager` for work
the user can watch, queue and stop.

Tasks run in *lanes*.  A lane runs one task at a time, in priority order, so
two things that must not overlap (two link downloads, two full scans) simply
queue, while different lanes proceed in parallel.  Every task reports its
life cycle (``queued`` → ``running`` → ``done``/``failed``/``cancelled``) to
the UI as ``task_state`` events.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import itertools
import threading
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Coroutine, Dict, List, Optional

from common.log import get_logger
from engine.events import EventBus

logger = get_logger(__name__)

QUEUED, RUNNING, STOPPING = "queued", "running", "stopping"
DONE, FAILED, CANCELLED = "done", "failed", "cancelled"
TERMINAL = (DONE, FAILED, CANCELLED)


class Runtime:
    """Owns the backend event loop thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="backend-loop", daemon=True)
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.call_soon(self._ready.set)
        self.loop.run_forever()

    @property
    def on_loop(self) -> bool:
        return threading.current_thread() is self._thread

    def submit(self, coro: Coroutine[Any, Any, Any]) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def call(self, coro: Coroutine[Any, Any, Any], timeout: Optional[float] = 60) -> Any:
        """Run ``coro`` on the loop and wait for its result (not from the loop)."""
        if self.on_loop:
            raise RuntimeError("Runtime.call must not be used on the backend loop")
        return self.submit(coro).result(timeout)

    def call_soon(self, fn: Callable[..., Any], *args: Any) -> None:
        self.loop.call_soon_threadsafe(fn, *args)

    def stop(self, timeout: float = 5.0) -> None:
        if not self.loop.is_running():
            return

        async def cancel_all() -> None:
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.wait(tasks, timeout=timeout) if tasks else None

        try:
            self.submit(cancel_all()).result(timeout + 1)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout)


@dataclass(eq=False)
class Task:
    name: str
    key: str
    scope: str
    lane: str
    priority: int
    factory: Callable[["Task"], Awaitable[Any]]
    kind: str = "user"
    state: str = QUEUED
    order: int = 0
    error: Optional[BaseException] = None
    result: Any = None
    handle: Optional[asyncio.Task] = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    progress: str = ""

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "key": self.key, "state": self.state, "scope": self.scope,
                "lane": self.lane, "kind": self.kind, "progress": self.progress}


class TaskManager:
    """Lanes of cancellable, de-duplicated tasks (loop-only API unless noted)."""

    def __init__(self, runtime: Runtime, events: EventBus):
        self.runtime = runtime
        self.events = events
        self._tasks: Dict[str, Task] = {}
        self._order = itertools.count()
        self._paused = asyncio.Event()
        self._paused.set()
        self._foreground = 0

    # ── pause (user downloads) ──────────────────────────────────────

    @property
    def paused(self) -> bool:
        return not self._paused.is_set()

    def pause(self) -> None:
        self._paused.clear()

    def resume(self) -> None:
        self._paused.set()

    async def wait_unpaused(self) -> None:
        await self._paused.wait()

    @property
    def foreground_busy(self) -> bool:
        """A user-started download is running (background downloads yield)."""
        return self._foreground > 0

    def hold_foreground(self) -> "_Foreground":
        return _Foreground(self)

    # ── submission ──────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Task]:
        task = self._tasks.get(key)
        return task if task is not None and task.state not in TERMINAL else None

    def submit(self, name: str, factory: Callable[[Task], Awaitable[Any]], *, scope: str,
               lane: str, key: str = "", priority: int = 10, kind: str = "user") -> Task:
        """Queue a task; an unfinished task with the same key is returned instead."""
        key = key or f"{name}:{next(self._order)}"
        existing = self.get(key)
        if existing is not None:
            return existing
        task = Task(name=name, key=key, scope=scope, lane=lane, priority=priority,
                    factory=factory, kind=kind, order=next(self._order))
        self._tasks[key] = task
        self._publish(task)
        self._dispatch()
        return task

    def submit_threadsafe(self, *args: Any, **kwargs: Any) -> Task:
        async def submit() -> Task:
            return self.submit(*args, **kwargs)

        return self.runtime.call(submit(), timeout=10)

    def _dispatch(self) -> None:
        busy = {task.lane for task in self._tasks.values() if task.state in (RUNNING, STOPPING)}
        waiting = sorted((task for task in self._tasks.values() if task.state == QUEUED),
                         key=lambda task: (task.priority, task.order))
        for task in waiting:
            if task.lane in busy:
                continue
            busy.add(task.lane)
            task.state = RUNNING
            self._publish(task)
            task.handle = asyncio.ensure_future(self._run(task))

    async def _run(self, task: Task) -> None:
        try:
            task.result = await task.factory(task)
            final = CANCELLED if task.state == STOPPING else DONE
        except asyncio.CancelledError:
            final = CANCELLED
        except Exception as exc:
            task.error = exc
            final = FAILED
            logger.exception("Task %s failed", task.key)
        self._finish(task, final)

    def _finish(self, task: Task, state: str) -> None:
        task.state = state
        task.done_event.set()
        self._publish(task)
        self._tasks.pop(task.key, None)
        self._dispatch()

    def _publish(self, task: Task) -> None:
        self.events.emit("task_state", task=task.name, state=task.state, scope=task.scope,
                         summary="", lane=task.lane)

    # ── control ─────────────────────────────────────────────────────

    def cancel(self, key: str) -> bool:
        task = self.get(key)
        if task is None:
            return False
        if task.state == QUEUED:
            self._finish(task, CANCELLED)
        elif task.state == RUNNING:
            task.state = STOPPING
            self._publish(task)
            if task.handle is not None:
                task.handle.cancel()
        return True

    def cancel_where(self, predicate: Callable[[Task], bool]) -> int:
        count = 0
        for task in list(self._tasks.values()):
            if task.state not in TERMINAL and predicate(task) and self.cancel(task.key):
                count += 1
        return count

    def states(self) -> List[Dict[str, Any]]:
        return [task.public() for task in self._tasks.values() if task.state not in TERMINAL]

    def pending(self, *names: str) -> bool:
        """A task with one of ``names`` is queued or running."""
        return any(task.name in names and task.state not in TERMINAL
                   for task in self._tasks.values())

    def running(self, lane: str) -> bool:
        return any(task.lane == lane and task.state in (RUNNING, STOPPING)
                   for task in self._tasks.values())

    async def shutdown(self) -> None:
        self.cancel_where(lambda _task: True)
        handles = [task.handle for task in list(self._tasks.values()) if task.handle]
        if handles:
            await asyncio.wait(handles, timeout=5)


class _Foreground:
    def __init__(self, manager: TaskManager):
        self.manager = manager

    def __enter__(self) -> "_Foreground":
        self.manager._foreground += 1
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.manager._foreground -= 1
