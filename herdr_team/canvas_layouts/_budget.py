"""Work and time budgets for layouts and routers (QA phase 2, R2).

Every layout and every batch of routes runs under a budget, so no op holds the
canvas lock for long whatever the graph. A budget has two limits:

* ``work``: deterministic units a caller counts (an A* state expanded, a pair of
  nodes pushed apart in one round of a force layout). Callers size their loops
  by it and take a cheap fallback when it runs out, so the same request gives
  the same result on every machine and on 3.9 and 3.14 alike.
* ``seconds``: a wall-clock ceiling, the safety net for a slow machine or a
  module that counts too little. Past it, a caller takes the same cheap
  fallback at once; the result is still a valid drawing, just a plainer one.

The budget in force is per thread (``active``), set by ``running``; code that
runs outside one gets an unlimited budget, so a layout or router called
directly behaves exactly as before.

Pure Python, stdlib only, 3.9.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, Optional


class Budget:
    """``work`` units (None: no cap) and a ``seconds`` ceiling (None: none), from now."""

    def __init__(self, work: Optional[int] = None, seconds: Optional[float] = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.work = work
        self.spent = 0
        self._clock = clock
        self.deadline = clock() + seconds if seconds is not None else None
        #: ``work`` or ``time``: what ran out first, once something did.
        self.exhausted: Optional[str] = None

    def left(self) -> Optional[int]:
        """The work units still to spend (None: no cap)."""
        return None if self.work is None else max(0, self.work - self.spent)

    def spend(self, units: int) -> None:
        self.spent += max(0, int(units))

    def over(self) -> bool:
        """Whether the work is spent or the ceiling has passed; the first reason sticks in ``exhausted``."""
        if self.exhausted is not None:
            return True
        if self.work is not None and self.spent >= self.work:
            self.exhausted = "work"
        elif self.deadline is not None and self._clock() >= self.deadline:
            self.exhausted = "time"
        return self.exhausted is not None

    def cap(self, most: int) -> int:
        """``most``, or the work left when that is less."""
        left = self.left()
        return most if left is None else min(most, left)


_LOCAL = threading.local()


def active() -> Budget:
    """The budget in force on this thread; an unlimited one outside ``running``."""
    found = getattr(_LOCAL, "budget", None)
    return found if found is not None else Budget()


@contextmanager
def running(budget: Budget) -> Iterator[Budget]:
    """Make ``budget`` the one in force on this thread while the block runs."""
    before = getattr(_LOCAL, "budget", None)
    _LOCAL.budget = budget
    try:
        yield budget
    finally:
        _LOCAL.budget = before
