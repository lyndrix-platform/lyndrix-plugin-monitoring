import asyncio
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from typing import Any, Dict, List, Optional

from ..model.models import _utc_now


@dataclass
class _ScheduledJobHandle:
    id: str
    task: asyncio.Task


class SimpleAsyncScheduler:
    def __init__(self):
        self._jobs: Dict[str, _ScheduledJobHandle] = {}
        self._started = False

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
    ):
        if replace_existing:
            self.remove_job(id)

        interval = max(1, seconds)
        delay = self._initial_delay(id, interval)

        async def runner():
            try:
                await asyncio.sleep(delay)
                while True:
                    await self._invoke(func, args or [])
                    await asyncio.sleep(interval)
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
                    await self._invoke(func, [])
            except asyncio.CancelledError:
                raise

        self._jobs[id] = _ScheduledJobHandle(id=id, task=asyncio.create_task(runner(), name=id))

    async def _invoke(self, func, args: List[Any]):
        result = func(*args)
        if asyncio.iscoroutine(result):
            await result

    def _initial_delay(self, job_id: str, interval_seconds: int) -> int:
        if interval_seconds <= 1:
            return 1
        digest = sha256(job_id.encode("utf-8")).digest()
        spread = max(1, min(interval_seconds, 30))
        return 1 + (digest[0] % spread)
