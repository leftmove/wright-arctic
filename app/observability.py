"""Bounded operation telemetry; never include tokens, SQL parameters or URLs."""

import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger("arctic.operations")


@dataclass
class Operation:
    started: float = field(default_factory=time.perf_counter)
    spans: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)

    def record(self, name: str, elapsed: float) -> None:
        self.spans[name] = self.spans.get(name, 0.0) + elapsed * 1000
        self.counts[name] = self.counts.get(name, 0) + 1


current_operation: ContextVar[Operation | None] = ContextVar("operation", default=None)


@contextmanager
def span(name: str) -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        operation = current_operation.get()
        if operation is not None:
            operation.record(name, time.perf_counter() - started)


def report(operation: Operation, *, route: str, status: int) -> None:
    elapsed = (time.perf_counter() - operation.started) * 1000
    # Log slow/errors and a bounded 1% sample. Process CPU belongs to interval
    # load measurements, not overlapping requests' individual wall spans.
    if status >= 500 or elapsed >= 500 or time.monotonic_ns() % 100 == 0:
        logger.info(
            json.dumps(
                {
                    "event": "operation",
                    "route": route,
                    "status": status,
                    "wall_ms": round(elapsed, 3),
                    "spans_ms": {
                        name: round(value, 3) for name, value in operation.spans.items()
                    },
                    "counts": operation.counts,
                }
            )
        )
