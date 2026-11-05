"""Bound expensive cover extraction without retaining any document/project state."""
from __future__ import annotations

from functools import wraps
from threading import BoundedSemaphore
from typing import Any, Callable

# One cover-extraction worker per API process, even during simultaneous exports.
# Parsing runs off the event loop; waiting requests do not spin or re-scan in parallel.
_SLOT = BoundedSemaphore(1)


def bounded_cover_work(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def limited(*args: Any, **kwargs: Any) -> Any:
        with _SLOT:
            return fn(*args, **kwargs)
    return limited
