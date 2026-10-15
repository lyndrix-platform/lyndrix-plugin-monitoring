import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any, Dict, List, Optional

from ..model.models import _utc_now


@dataclass
class _ScheduledJobHandle:
    id: str
    task: asyncio.Task


_log = logging.getLogger("lyndrix.plugin.state_monitoring.scheduler")


class SimpleAsyncScheduler:
    # Extra pause after N consecutive failures of one job: 2, 4, 8 … capped.
    FAILURE_BACKOFF_CAP = 60

    def __init__(self, log=None):
        self._jobs: Dict[str, _ScheduledJobHandle] = {}
        self._started = False
        self._log = log or _log

    def start(self):
        self._started = True

    def shutdown(self):
        for job_id in list(self._jobs):
            self.remove_job(job_id)
        self._started = False

    def get_job(self, job_id: str) -> Optional[_ScheduledJobHandle]:
        handle = self._jobs.get(job_id)
        if handle and handle.task.done():
            self._jobs.pop(job_id, None)
            return None
        return handle

    def get_jobs(self) -> List[_ScheduledJobHandle]:
        active = []
        for job_id in list(self._jobs):
            handle = self.get_job(job_id)
            if handle:
                active.append(handle)
        return active

    def remove_job(self, job_id: str):
        handle = self._jobs.pop(job_id, None)
        if handle:
            handle.task.cancel()

    def add_interval_job(
        self,
        func,
        *,
        seconds: int,
        args: Optional[List[Any]] = None,
        id: str,
        replace_existing: bool = True,
        initial_delay: Optional[int] = None,
    ):
        if replace_existing:
            self.remove_job(id)

        interval = max(1, seconds)
        # Explicit first-run delay (e.g. "one hour after start" for housekeeping
        # that must not compete with the startup sweep); default = jittered.
        delay = max(1, int(initial_delay)) if initial_delay is not None else self._initial_delay(id, interval)

        async def runner():
            failures = 0
            try:
                await asyncio.sleep(delay)
                while True:
                    # One failed run (DB error, lost write) must never end the job.
                    failures = 0 if await self._invoke_safely(id, func, args or []) else failures + 1
                    await asyncio.sleep(interval + self._failure_backoff(failures))
            except asyncio.CancelledError:
                raise

        self._jobs[id] = _ScheduledJobHandle(id=id, task=asyncio.create_task(runner(), name=id))

    def add_daily_job(self, func, *, hour: int, minute: int, id: str, replace_existing: bool = True):
        if replace_existing:
            self.remove_job(id)

        async def runner():
            try:
                while True:
                    now = _utc_now()
                    next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if next_run <= now:
                        next_run += timedelta(days=1)
                    await asyncio.sleep(max(1, int((next_run - now).total_seconds())))
                    await self._invoke_safely(id, func, [])
            except asyncio.CancelledError:
                raise

        self._jobs[id] = _ScheduledJobHandle(id=id, task=asyncio.create_task(runner(), name=id))

    async def _invoke(self, func, args: List[Any]):
        result = func(*args)
        if asyncio.iscoroutine(result):
            await result

    async def _invoke_safely(self, job_id: str, func, args: List[Any]) -> bool:
        """Run one job iteration; log and swallow anything but cancellation."""
        try:
            await self._invoke(func, args)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                self._log.warning(f"State Monitoring: scheduled job '{job_id}' failed: {exc}")
            except Exception:
                pass
            return False

    def _failure_backoff(self, failures: int) -> int:
        if failures < 2:
            return 0
        return min(self.FAILURE_BACKOFF_CAP, 2 ** (failures - 1))

    def _initial_delay(self, job_id: str, interval_seconds: int) -> int:
        if interval_seconds <= 1:
            return 1
        digest = sha256(job_id.encode("utf-8")).digest()
        spread = max(1, min(interval_seconds, 30))
        return 1 + (digest[0] % spread)
