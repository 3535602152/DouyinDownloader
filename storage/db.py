"""The application's one SQLite database.

All writes run on a single dedicated writer thread with one connection, one
transaction per call, so there is never lock contention between features.
Reads run on a small pool of read connections (WAL mode lets them proceed
while a write is in progress).  Both are exposed as coroutines for the
backend event loop and as blocking calls for any other thread.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

from common.log import get_logger

logger = get_logger(__name__)

T = TypeVar("T")
JOURNAL_SIZE_LIMIT = 64 * 1024 * 1024


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None,
                                 check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(f"PRAGMA journal_size_limit = {JOURNAL_SIZE_LIMIT}")
    return connection


class Database:
    def __init__(self, path: Path | str):
        self.path = str(Path(path))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-writer")
        self._readers = ThreadPoolExecutor(max_workers=4, thread_name_prefix="db-reader")
        self._local = threading.local()
        self._write_connection: Optional[sqlite3.Connection] = None
        self._closed = False
        self.write_seconds = 0.0
        self._writer.submit(self._open_writer).result()

    # ── connections ─────────────────────────────────────────────────

    def _open_writer(self) -> None:
        from storage import schema

        connection = _connect(self.path)
        connection.execute("PRAGMA journal_mode = WAL")
        schema.migrate(connection, Path(self.path))
        self._write_connection = connection

    def _reader(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = _connect(self.path)
            connection.execute("PRAGMA query_only = ON")
            self._local.connection = connection
            self._local.all = getattr(self._local, "all", [])
            self._local.all.append(connection)
        return connection

    def _run_write(self, fn: Callable[..., T], args, kwargs) -> T:
        connection = self._write_connection
        if connection is None:
            raise RuntimeError("database is closed")
        started = time.monotonic()
        connection.execute("BEGIN IMMEDIATE")
        try:
            result = fn(connection, *args, **kwargs)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")
        self.write_seconds += time.monotonic() - started
        return result

    def _run_read(self, fn: Callable[..., T], args, kwargs) -> T:
        return fn(self._reader(), *args, **kwargs)

    # ── public API ──────────────────────────────────────────────────

    async def write(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._writer, self._run_write, fn, args, kwargs)

    async def read(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._readers, self._run_read, fn, args, kwargs)

    def write_sync(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self._writer.submit(self._run_write, fn, args, kwargs).result()

    def read_sync(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        return self._readers.submit(self._run_read, fn, args, kwargs).result()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        def close_writer() -> None:
            if self._write_connection is not None:
                try:
                    self._write_connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
                except sqlite3.Error:
                    pass
                self._write_connection.close()
                self._write_connection = None

        try:
            self._writer.submit(close_writer).result(timeout=10)
        except Exception:
            pass
        self._writer.shutdown(wait=False)
        self._readers.shutdown(wait=False)


def rows(connection: sqlite3.Connection, sql: str, params=()) -> list:
    return [dict(row) for row in connection.execute(sql, params)]


def one(connection: sqlite3.Connection, sql: str, params=()) -> Optional[dict]:
    row = connection.execute(sql, params).fetchone()
    return dict(row) if row else None


def scalar(connection: sqlite3.Connection, sql: str, params=(), default: Any = 0) -> Any:
    row = connection.execute(sql, params).fetchone()
    return default if row is None or row[0] is None else row[0]


def marks(values) -> str:
    return ",".join("?" for _ in values)


def chunks(values, size: int = 500):
    values = list(values)
    for start in range(0, len(values), size):
        yield values[start:start + size]
