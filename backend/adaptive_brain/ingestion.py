from __future__ import annotations
import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable
from .memory import BrainMemory
from .schema import Observation

@dataclass(frozen=True)
class FeedSource:
    source_id: str
    interval_seconds: float
    poll: Callable[[], Awaitable[list[Observation]]]

class AdaptiveFeedHub:
    """Always-on framework for explicitly approved network sources."""
    def __init__(self, memory: BrainMemory):
        self.memory=memory; self._sources={}; self._tasks={}; self._running=False

    def register(self, source: FeedSource):
        if not source.source_id.strip(): raise ValueError("source_id required")
        if source.interval_seconds < 1: raise ValueError("interval_seconds must be >=1")
        self._sources[source.source_id]=source

    async def _run(self, source: FeedSource):
        while self._running:
            try:
                for obs in await source.poll(): self.memory.add_observation(obs)
            except asyncio.CancelledError: raise
            except Exception: pass
            await asyncio.sleep(source.interval_seconds)

    async def start(self):
        if self._running: return
        self._running=True
        for s in self._sources.values(): self._tasks[s.source_id]=asyncio.create_task(self._run(s))

    async def stop(self):
        self._running=False
        tasks=list(self._tasks.values())
        for t in tasks: t.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        self._tasks.clear()

    def status(self):
        return {"running":self._running,"registered_sources":sorted(self._sources),"active_tasks":sorted(k for k,v in self._tasks.items() if not v.done())}
