"""Record real model responses once; replay them forever.

Two things depend on this and neither works without it:

**A gate needs determinism.** If the same dataset scores 0.82 then 0.79 then
0.81 with no change to the code, a threshold cannot distinguish a regression
from sampling noise. Temperature 0 narrows that but does not close it — providers
change model weights behind a stable name, and a "no change" run months later is
not comparing what it thinks it is.

**A load test needs to be free.** A thousand requests against gpt-4o costs real
money and takes real time. The same thousand against a cassette costs nothing and
runs at whatever rate the process can manage — which is the point, since the
thing being measured is the platform, not the provider.

## The modes

``off``
    Straight through to the provider. Normal operation.

``record``
    Calls the provider and writes each response to disk, keyed by a hash of the
    request. Existing entries are not overwritten, so re-recording adds new
    interactions without silently changing old ones.

``replay``
    Reads from disk and **never touches the network**. A miss is an error, not a
    fall-through. A cassette that quietly calls the provider on a miss is worse
    than no cassette: it spends money during a load test and destroys the
    determinism a gate depends on, both invisibly.

## The key

A SHA-256 over the model, messages, temperature and tools — everything that can
change the answer. The tenant is deliberately **not** in the key: a cassette is a
recording of the *provider*, and keying by tenant would force one recording per
tenant for identical questions. Tenant isolation is enforced on the data, not on
the recording of a model's reply to a fixed prompt.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from platform_core.ports.llm import ChatRequest, ChatResponse, TokenUsage
from platform_core.settings import get_settings

logger = logging.getLogger("platform.gates.cassette")


class CassetteMiss(RuntimeError):
    """Replay was asked for an interaction that was never recorded.

    An error rather than a fall-through, deliberately. Falling through to the
    provider would spend money during a load test and break determinism during a
    gate — both silently, and both at exactly the moment the guarantee mattered.
    """

    def __init__(self, key: str, request: ChatRequest) -> None:
        self.key = key
        super().__init__(
            f"no recorded interaction for {key[:12]}… (model={request.model}, "
            f"{len(request.messages)} messages). Re-record with "
            f"CASSETTE_MODE=record; replay must never reach the network."
        )


def interaction_key(request: ChatRequest) -> str:
    """Everything that can change the answer, and nothing that cannot."""
    payload = json.dumps(
        {
            "model": request.model,
            "messages": request.messages,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "tools": request.tools,
            "response_format": request.response_format,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Interaction:
    key: str
    model: str
    content: str
    input_tokens: int
    output_tokens: int
    finish_reason: str

    def to_response(self, *, cost_usd: float) -> ChatResponse:
        return ChatResponse(
            content=self.content,
            model=self.model,
            usage=TokenUsage(self.input_tokens, self.output_tokens, reported=True),
            cost_usd=cost_usd,
            finish_reason=self.finish_reason,
            cache_hit=False,
            attempts=1,
            cassette_key=self.key,
        )


class Cassette:
    """A directory of recorded interactions, one JSON file per key."""

    def __init__(self, directory: Path | None = None, mode: str | None = None) -> None:
        settings = get_settings()
        self.directory = Path(directory or settings.cassette_dir)
        self.mode = mode or settings.cassette_mode
        self._misses: list[str] = []
        self._hits = 0
        self._recorded = 0

    def _path(self, key: str) -> Path:
        # Two-level fan-out: a flat directory of tens of thousands of files is
        # slow to list and unpleasant to inspect in git.
        return self.directory / key[:2] / f"{key}.json"

    def get(self, request: ChatRequest) -> Interaction | None:
        key = interaction_key(request)
        path = self._path(key)
        if not path.exists():
            self._misses.append(key)
            return None
        data = json.loads(path.read_text())
        self._hits += 1
        return Interaction(
            key=key, model=data["model"], content=data["content"],
            input_tokens=data["input_tokens"], output_tokens=data["output_tokens"],
            finish_reason=data.get("finish_reason", "stop"),
        )

    def put(self, request: ChatRequest, response: ChatResponse) -> str:
        key = interaction_key(request)
        path = self._path(key)
        if path.exists():
            # Never overwrite. Re-recording should add interactions, not
            # silently change the answers a historical run was scored against.
            return key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "key": key,
                    "model": response.model,
                    "content": response.content,
                    "input_tokens": response.usage.input_tokens,
                    "output_tokens": response.usage.output_tokens,
                    "finish_reason": response.finish_reason,
                    # The request is stored for readability, so a reviewer can
                    # see what was asked without recomputing the hash.
                    "request": {
                        "model": request.model,
                        "messages": request.messages,
                        "temperature": request.temperature,
                    },
                },
                indent=2,
                default=str,
            )
        )
        self._recorded += 1
        return key

    def content_sha(self) -> str:
        """A digest of the whole cassette, recorded on every eval run.

        Lets "the score changed" be separated from "the recorded answers
        changed underneath us" — without it, a re-recorded cassette looks
        exactly like a code regression.
        """
        digest = hashlib.sha256()
        for path in sorted(self.directory.rglob("*.json")):
            digest.update(path.name.encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()

    def stats(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "hits": self._hits,
            "recorded": self._recorded,
            "misses": len(self._misses),
            "interactions": sum(1 for _ in self.directory.rglob("*.json")),
        }


class CassetteLLM:
    """Wraps an :class:`InstrumentedLLM`, intercepting at the provider boundary.

    Deliberately wraps the *whole* instrumented client rather than sitting inside
    it. Replay must still pass through identity, budget and metering — a replayed
    call is a call, and a load test whose calls skip the budget check is not
    testing the platform under load, it is testing a different platform.

    The cost recorded for a replayed interaction is the cost the real call would
    have had, computed from the recorded token counts. That keeps a load test's
    cost projection meaningful even though nothing was spent.
    """

    def __init__(self, inner, cassette: Cassette) -> None:
        self._inner = inner
        self._cassette = cassette

    def chat(self, ctx, request: ChatRequest) -> ChatResponse:
        from platform_core.observability.llm import price

        mode = self._cassette.mode

        if mode == "replay":
            recorded = self._cassette.get(request)
            if recorded is None:
                raise CassetteMiss(interaction_key(request), request)
            cost = price(recorded.model, recorded.input_tokens, recorded.output_tokens)
            # Metered exactly like a live call: a replayed call still consumes
            # budget and still produces a ledger row, so load tests exercise the
            # accounting rather than bypassing it.
            self._inner._record(
                ctx, model=recorded.model, input_tokens=recorded.input_tokens,
                output_tokens=recorded.output_tokens, cache_hit=False,
                usage_reported=True, cost=cost,
            )
            return recorded.to_response(cost_usd=cost)

        response = self._inner.chat(ctx, request)
        if mode == "record":
            self._cassette.put(request, response)
        return response

    def embed(self, ctx, texts: list[str], *, model: str | None = None):
        return self._inner.embed(ctx, texts, model=model)

    def token_count(self, text: str, *, model: str | None = None) -> int:
        return self._inner.token_count(text, model=model)
