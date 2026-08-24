"""Telemetry — the measurement spine (walkthrough §5).

Minimal timing spans so every read/act/emit is measurable from day one. Records
are kept in-memory and can be drained by a deployment exporter. No external deps.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Span:
    name: str
    duration_ms: float
    ok: bool
    ts_ms: int


@dataclass
class Telemetry:
    service: str
    _spans: list[Span] = field(default_factory=list)

    @contextmanager
    def span(self, name: str) -> Iterator[None]:
        start = time.perf_counter()
        ok = True
        try:
            yield
        except Exception:
            ok = False
            raise
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            self._spans.append(
                Span(
                    name=name,
                    duration_ms=duration_ms,
                    ok=ok,
                    ts_ms=int(time.time() * 1000),
                )
            )

    def drain(self) -> list[Span]:
        """Return and clear collected spans (for an exporter to ship)."""
        spans, self._spans = self._spans, []
        return spans
