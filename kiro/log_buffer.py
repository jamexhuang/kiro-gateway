# -*- coding: utf-8 -*-

"""In-memory ring buffer for log lines, plus loguru sink + pub/sub fanout."""

from __future__ import annotations

from collections import deque
from itertools import count
from threading import RLock
from typing import Any, Callable, Deque, Dict, List, Optional


class LogRingBuffer:
    """Thread-safe ring buffer of recent log events with subscriber fanout."""

    def __init__(self, capacity: int = 2000):
        self._lock = RLock()
        self._entries: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._seq = count()
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []

    def append(self, entry: Dict[str, Any]) -> None:
        if "seq" not in entry:
            entry = {**entry, "seq": next(self._seq)}
        subs: List[Callable[[Dict[str, Any]], None]]
        with self._lock:
            self._entries.append(entry)
            subs = list(self._subscribers)
        for fn in subs:
            try:
                fn(entry)
            except Exception:
                pass

    def snapshot(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            data = list(self._entries)
        return data if limit is None else data[-limit:]

    def since(self, seq: int) -> List[Dict[str, Any]]:
        with self._lock:
            return [e for e in self._entries if e.get("seq", -1) > seq]

    def subscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            self._subscribers.append(fn)

    def unsubscribe(self, fn: Callable[[Dict[str, Any]], None]) -> None:
        with self._lock:
            try:
                self._subscribers.remove(fn)
            except ValueError:
                pass


log_buffer = LogRingBuffer(capacity=2000)


def loguru_sink(message) -> None:
    """Loguru sink that pushes each formatted record into the ring buffer."""
    record = message.record
    log_buffer.append({
        "ts": record["time"].timestamp(),
        "level": record["level"].name,
        "name": record["name"],
        "function": record["function"],
        "line": record["line"],
        "msg": record["message"],
    })
