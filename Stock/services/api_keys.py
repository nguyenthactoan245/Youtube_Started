"""Shared, thread-safe API scheduling; never exposes credentials in status messages."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from threading import Event, Lock


@dataclass
class KeyState:
    ready_at: float = 0
    busy: bool = False
    invalid: bool = False
    remaining: int | None = None


class ApiKeyPool:
    def __init__(self, keys=(), *, interval=0.75):
        self._lock = Lock()
        self._states = {}
        self._cursor = 0
        self._next_request = 0.0
        self.interval = interval
        self.pause_reason = ''
        self._probe = False
        self.auto_retrying = False
        self.configure(keys)

    def resume(self):
        with self._lock:
            if self.pause_reason and not self.auto_retrying:
                self.pause_reason = ''
                self._probe = True

    def configure(self, keys):
        with self._lock:
            keys = list(dict.fromkeys(keys))
            if keys != list(self._states):
                self._states = {key: self._states.get(key, KeyState()) for key in keys}
                self._cursor = 0

    def __bool__(self):
        with self._lock:
            return bool(self._states)

    def statuses(self):
        with self._lock:
            if self.pause_reason:
                return [self.pause_reason]
            now = time.monotonic()
            return [f"Key {i}: " + (
                "cần kiểm tra" if state.invalid else
                "đang gọi API" if state.busy else
                f"chờ {math.ceil(state.ready_at - now)} giây" if state.ready_at > now else
                "sẵn sàng") for i, state in enumerate(self._states.values(), 1)]

    def fetch(self, request, cancel=None):
        # Imported here so pixabay can also accept the pool without a module cycle.
        from services.pixabay import PixabayError, PixabayRateLimitError, PixabayAccessPause
        cancel = cancel if cancel is not None else Event()
        failures = 0
        while True:
            if cancel.is_set():
                raise InterruptedError("Đã hủy trong khi chờ API.")
            with self._lock:
                now = time.monotonic()
                entries = list(self._states.items())
                if not entries:
                    raise PixabayError("Chưa có PIXABAY_API_KEYS. Hãy thêm key trong Setup.")
                if all(s.invalid for _, s in entries):
                    raise PixabayError("Các API key đều cần kiểm tra. Hãy sửa key trong Setup.")
                selected = None
                if not self.pause_reason and now >= self._next_request and not (
                        self._probe and any(s.busy for _, s in entries)):
                    for offset in range(len(entries)):
                        index = (self._cursor + offset) % len(entries)
                        key, state = entries[index]
                        if not state.invalid and not state.busy and state.ready_at <= now:
                            selected = key, state
                            state.busy = True
                            self._cursor = (index + 1) % len(entries)
                            self._next_request = now + self.interval
                            break
            if selected is None:
                cancel.wait(0.1)
                continue
            key, state = selected

            def headers_received(headers):
                from services.pixabay import rate_limit_delay
                try:
                    remaining = int(headers.get("X-RateLimit-Remaining", ""))
                except (ValueError, TypeError):
                    return
                with self._lock:
                    state.remaining = remaining
                    if remaining <= 0:
                        state.ready_at = time.monotonic() + rate_limit_delay(headers)

            try:
                result = request(key, headers_received)
                with self._lock:
                    self._probe = False
                return result
            except PixabayAccessPause as exc:
                with self._lock:
                    self.pause_reason = str(exc)
                    self.auto_retrying = True
                    # Keep queued searches blocked while the single automatic retry waits.
                    self._probe = True
                cancelled = cancel.wait(5)
                with self._lock:
                    self.pause_reason = ''
                    self.auto_retrying = False
                    self._probe = True
                if cancelled:
                    raise InterruptedError("ÄÃ£ há»§y trong khi chá» API.") from None
            except PixabayRateLimitError as exc:
                failures += 1
                with self._lock:
                    state.ready_at = time.monotonic() + exc.retry_after
                if failures >= 3 * max(1, len(entries)):
                    raise
            except PixabayError as exc:
                if getattr(exc, "invalid_key", False):
                    with self._lock:
                        state.invalid = True
                else:
                    raise
            finally:
                with self._lock:
                    state.busy = False


_shared_pool = ApiKeyPool()


def get_api_key_pool():
    from app_config import configured_api_keys
    _shared_pool.configure(configured_api_keys())
    return _shared_pool
