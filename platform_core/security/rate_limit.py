"""Distributed, privacy-preserving fixed-window admission control.

Production uses Redis so a limit applies to the tenant or identity rather than
to whichever replica happened to receive a request. Local and test processes
fall back to a bounded in-memory window when Redis is absent; deployed
environments fail closed, as required by :class:`Settings` coherence checks.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

from redis import Redis
from redis.exceptions import RedisError

from platform_core.settings import Settings, get_settings

_WINDOW_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
local ttl = redis.call('TTL', KEYS[1])
return {count, ttl}
"""


@dataclass(frozen=True, slots=True)
class LimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int


class RateLimitUnavailable(RuntimeError):
    """The distributed limiter could not make a production decision."""


class RateLimiter:
    def __init__(self, settings: Settings, client: Redis | None = None) -> None:
        self._settings = settings
        self._client = client or Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
        )
        self._local: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def check(self, bucket: str, subject: str, *, limit: int, window_seconds: int) -> LimitDecision:
        if not self._settings.rate_limit_enabled:
            return LimitDecision(True, limit, limit, 0)

        key = self._key(bucket, subject, window_seconds)
        try:
            count, ttl = self._client.eval(_WINDOW_SCRIPT, 1, key, window_seconds)
            return self._decision(int(count), int(ttl), limit, window_seconds)
        except RedisError as exc:
            if self._settings.environment in {"staging", "production"}:
                if self._settings.rate_limit_fail_closed:
                    raise RateLimitUnavailable("distributed rate limiter unavailable") from exc
                return LimitDecision(True, limit, limit, 0)
            return self._check_local(key, limit=limit, window_seconds=window_seconds)

    def _key(self, bucket: str, subject: str, window_seconds: int) -> str:
        secret = self._settings.jwt_secret.get_secret_value().encode("utf-8")
        digest = hmac.new(secret, subject.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"platform:limit:{bucket}:{window_seconds}:{digest}"

    @staticmethod
    def _decision(count: int, ttl: int, limit: int, window_seconds: int) -> LimitDecision:
        retry_after = max(1, ttl if ttl > 0 else window_seconds)
        return LimitDecision(
            allowed=count <= limit,
            limit=limit,
            remaining=max(0, limit - count),
            retry_after_seconds=retry_after if count > limit else 0,
        )

    def _check_local(self, key: str, *, limit: int, window_seconds: int) -> LimitDecision:
        now = time.monotonic()
        with self._lock:
            count, expires_at = self._local.get(key, (0, now + window_seconds))
            if expires_at <= now:
                count, expires_at = 0, now + window_seconds
            count += 1
            self._local[key] = (count, expires_at)

            # Bound fallback memory even under a subject-spraying login attack.
            if len(self._local) > 10_000:
                self._local = {
                    candidate: value
                    for candidate, value in self._local.items()
                    if value[1] > now
                }

        ttl = max(1, int(expires_at - now))
        return self._decision(count, ttl, limit, window_seconds)


@lru_cache(maxsize=1)
def get_rate_limiter() -> RateLimiter:
    return RateLimiter(get_settings())


def reset_rate_limiter() -> None:
    """Drop cached state after tests replace settings or Redis."""
    get_rate_limiter.cache_clear()
