from __future__ import annotations

import secrets
import threading
import time

_ENCODING = "0123456789abcdefghjkmnpqrstvwxyz"
_LOCK = threading.Lock()
_LAST_MS = -1
_LAST_RANDOM = 0


def _encode(value, length):
    chars = []
    for _ in range(length):
        chars.append(_ENCODING[value & 31])
        value >>= 5
    return "".join(reversed(chars))


def monotonic_ulid():
    global _LAST_MS, _LAST_RANDOM

    now_ms = time.time_ns() // 1_000_000
    with _LOCK:
        if now_ms == _LAST_MS:
            _LAST_RANDOM += 1
            if _LAST_RANDOM >= (1 << 80):
                raise OverflowError("ULID randomness overflow")
        else:
            _LAST_MS = now_ms
            _LAST_RANDOM = secrets.randbits(80)
        return _encode(now_ms, 10) + _encode(_LAST_RANDOM, 16)
