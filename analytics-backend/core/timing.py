"""
Per-stage latency instrumentation (Phase 1 of the refinement plan).

Motivation: before optimizing anything, know where the time actually goes.
RequestLoggingMiddleware already times whole requests; this module breaks a
single AI-analyst request into its pipeline stages so we can see p50/p90 per
stage (embed → search → context build → LLM first token → stream total)
instead of one opaque number.

Usage:
    from core.timing import StageTimer

    t = StageTimer("chat_stream")
    with t.stage("retrieve"):
        sources = retrieve(...)
    t.mark("llm_first_token")          # point-in-time marks also supported
    ...
    t.log()   # -> one structured line: TIMING chat_stream retrieve=0.213s ...

Design notes:
- Stdlib only, no deps; time.perf_counter for monotonic timing.
- Never raises: instrumentation must not be able to break the request path.
- One log line per request keeps it grep-able:  `grep TIMING` and you have a
  dataset you can paste into pandas for p50/p90.
"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

log = logging.getLogger("kapi.timing")


class StageTimer:
    """Accumulates named stage durations for one logical request."""

    def __init__(self, label: str):
        self.label = label
        self._t0 = time.perf_counter()
        self._stages: list[tuple[str, float]] = []

    @contextmanager
    def stage(self, name: str):
        """Time a block: `with timer.stage("retrieve"): ...`"""
        start = time.perf_counter()
        try:
            yield
        finally:
            self._stages.append((name, time.perf_counter() - start))

    def mark(self, name: str) -> None:
        """Record time elapsed since timer creation as a point-in-time mark."""
        self._stages.append((name, time.perf_counter() - self._t0))

    def total(self) -> float:
        return time.perf_counter() - self._t0

    def log(self) -> None:
        """Emit one structured log line. Never raises."""
        try:
            parts = " ".join(f"{name}={dur:.3f}s" for name, dur in self._stages)
            log.info("TIMING %s %s total=%.3fs", self.label, parts, self.total())
        except Exception:
            pass
