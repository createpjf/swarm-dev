"""
core/async_wrappers.py — Async wrappers for file-locked data structures.

When running in InProcessRuntime, all agents share a single event loop.
``TaskBoard`` uses ``filelock.FileLock`` which is a blocking call —
if an agent holds the lock while another agent's coroutine yields,
the event loop can deadlock.

``AsyncTaskBoardWrapper`` pushes every lock-holding operation into
``asyncio.to_thread()`` (i.e. ``loop.run_in_executor(None, ...)``)
so the event loop is never blocked.

Usage::

    board = TaskBoard()
    async_board = AsyncTaskBoardWrapper(board)
    task = await async_board.create("do something")
    claimed = await async_board.claim("jerry")
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class AsyncTaskBoardWrapper:
    """Async wrapper that delegates blocking TaskBoard ops to threads.

    Wraps ONLY the methods that acquire ``self.lock`` (FileLock).
    Non-locking reads (e.g. ``collect_results``) are passed through directly.
    """

    def __init__(self, board):
        """
        Args:
            board: A ``TaskBoard`` instance (the real, synchronous one).
        """
        self._board = board

    # ── Passthrough for non-locking attributes ───────────────────────────

    def __getattr__(self, name: str):
        """Proxy non-wrapped attributes to the underlying board."""
        return getattr(self._board, name)

    # ── Async wrappers for lock-acquiring methods ────────────────────────

    async def create(self, description: str, **kwargs):
        """Async wrapper for board.create()."""
        return await asyncio.to_thread(
            self._board.create, description, **kwargs)

    async def claim(self, agent_id: str, agent_role: str = ""):
        """Async wrapper for board.claim()."""
        return await asyncio.to_thread(
            self._board.claim, agent_id, agent_role)

    async def complete(self, task_id: str, result: str = ""):
        """Async wrapper for board.complete()."""
        return await asyncio.to_thread(
            self._board.complete, task_id, result)

    async def fail(self, task_id: str, error: str = ""):
        """Async wrapper for board.fail()."""
        return await asyncio.to_thread(
            self._board.fail, task_id, error)

    async def submit_for_review(self, task_id: str, result: str = ""):
        """Async wrapper for board.submit_for_review()."""
        return await asyncio.to_thread(
            self._board.submit_for_review, task_id, result)

    async def review_complete(self, task_id: str, score: int = 0,
                              comment: str = ""):
        """Async wrapper for board.review_complete()."""
        return await asyncio.to_thread(
            self._board.review_complete, task_id, score, comment)

    async def recover_stale_tasks(self):
        """Async wrapper for board.recover_stale_tasks()."""
        return await asyncio.to_thread(self._board.recover_stale_tasks)

    async def clear(self, **kwargs):
        """Async wrapper for board.clear()."""
        return await asyncio.to_thread(self._board.clear, **kwargs)

    async def get(self, task_id: str):
        """Async wrapper for board.get()."""
        return await asyncio.to_thread(self._board.get, task_id)

    async def cancel(self, task_id: str):
        """Async wrapper for board.cancel()."""
        return await asyncio.to_thread(self._board.cancel, task_id)

    async def pause(self, task_id: str):
        """Async wrapper for board.pause()."""
        return await asyncio.to_thread(self._board.pause, task_id)

    async def resume(self, task_id: str):
        """Async wrapper for board.resume()."""
        return await asyncio.to_thread(self._board.resume, task_id)

    async def retry(self, task_id: str):
        """Async wrapper for board.retry()."""
        return await asyncio.to_thread(self._board.retry, task_id)

    # ── Sync passthrough for read-only / non-locking ops ─────────────────
    # These are safe to call synchronously even in async context because
    # they just read the in-memory path or don't acquire the file lock.

    def collect_results(self, *args, **kwargs):
        return self._board.collect_results(*args, **kwargs)

    @property
    def path(self):
        return self._board.path

    @property
    def lock(self):
        """Expose underlying lock for code that does manual locking.

        WARNING: In InProcess mode, manual ``with board.lock:`` blocks
        the event loop.  Prefer using the async methods above.
        """
        return self._board.lock
