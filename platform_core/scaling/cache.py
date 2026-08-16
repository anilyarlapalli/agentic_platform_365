"""Semantic cache, scoped to a tenant by key rather than by a check on read.

The tenant id is **in the key**, not compared after the fetch. A shared key space
with a check afterwards has two failure modes that a scoped key does not: a
missing check serves another tenant's answer, and a cache-fill race can populate
a key one tenant then reads. Both produce a correct-looking wrong answer that no
error surfaces — the worst kind of cache bug.

`core/semantic_cache.py` in the source tree says "swap the backing store to Redis
without changing the API" and nothing ever has, so identical queries across
replicas each pay full retrieval and a full LLM call. This is that swap, with the
tenant boundary added.

## Exact keys, not similarity

Deliberately keyed on an exact hash of the request rather than on embedding
similarity. A similarity cache answers a question that was *nearly* asked, which
is a correctness decision disguised as an optimisation — and the threshold that
governs it is impossible to tune without a golden set for the cache itself.
Exact-match caching of identical requests is unambiguous and still removes the
duplicate traffic that matters.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

from platform_core.identity.principal import RequestContext
from platform_core.ports.llm import ChatResponse, TokenUsage
from platform_core.settings import get_settings

logger = logging.getLogger("platform.scaling.cache")

DEFAULT_TTL_SECONDS = 3600


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    errors: int = 0

    @property
    def hit_ratio(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


class RedisSemanticCache:
    """Tenant-scoped response cache over Redis.

    Every failure mode degrades to a miss. A cache that can take the request
    path down when it is unavailable is a new dependency pretending to be an
    optimisation — the whole point is that removing it changes cost and latency,
    never correctness or availability.
    """

    def __init__(self, client=None, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._client = client
        self._ttl = ttl_seconds
        self.stats = CacheStats()

    def _redis(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                get_settings().redis_url,
                socket_timeout=0.25,
                socket_connect_timeout=0.25,
                decode_responses=True,
            )
        return self._client

    @staticmethod
    def key(ctx: RequestContext, payload: dict[str, Any]) -> str:
        """Tenant-scoped key. The tenant is a *prefix*, not a stored field.

        A prefix means a cross-tenant hit is not merely rejected — it is
        unreachable, because the key another tenant would compute is a different
        string. It also makes `DEL platform:cache:<tenant>:*` a complete,
        obviously-correct per-tenant invalidation.
        """
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        return f"platform:cache:{ctx.tenant.id}:{digest}"

    def get(self, ctx: RequestContext, cache_key: str) -> dict | None:
        try:
            raw = self._redis().get(cache_key)
        except Exception:
            # A miss, not an error. Degrading to the real call is always correct.
            self.stats.errors += 1
            logger.debug("cache read failed — treating as a miss", exc_info=True)
            return None

        if raw is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        try:
            return json.loads(raw)
        except Exception:
            self.stats.errors += 1
            return None

    def put(self, ctx: RequestContext, cache_key: str, response: ChatResponse) -> None:
        try:
            self._redis().setex(
                cache_key,
                self._ttl,
                json.dumps(
                    {
                        "content": response.content,
                        "model": response.model,
                        "usage": {
                            "input_tokens": response.usage.input_tokens,
                            "output_tokens": response.usage.output_tokens,
                            "reported": response.usage.reported,
                        },
                        "cost_usd": response.cost_usd,
                        "finish_reason": response.finish_reason,
                    }
                ),
            )
        except Exception:
            self.stats.errors += 1
            logger.debug("cache write failed — the answer is unaffected", exc_info=True)

    def invalidate_tenant(self, ctx: RequestContext) -> int:
        """Drop everything for one tenant. Complete, because the key is prefixed.

        `scan_iter` rather than `KEYS`: the latter blocks the server for the
        duration of a full keyspace walk, which on a shared Redis is an outage
        for every other user of it.
        """
        removed = 0
        try:
            client = self._redis()
            for key in client.scan_iter(match=f"platform:cache:{ctx.tenant.id}:*", count=500):
                client.delete(key)
                removed += 1
        except Exception:
            logger.warning("tenant cache invalidation failed", exc_info=True)
        return removed


def response_from_cached(data: dict) -> ChatResponse:
    usage = data.get("usage", {})
    return ChatResponse(
        content=data["content"],
        model=data["model"],
        usage=TokenUsage(
            usage.get("input_tokens", 0), usage.get("output_tokens", 0),
            reported=usage.get("reported", True),
        ),
        # Zero, because nothing was spent on this request. The original call's
        # cost is already in the ledger; charging it again would double-count
        # spend and make the cache look like it increased cost.
        cost_usd=0.0,
        finish_reason=data.get("finish_reason", "stop"),
        cache_hit=True,
    )
