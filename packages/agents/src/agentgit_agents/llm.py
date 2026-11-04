"""A thin, provider-neutral LLM interface.

The agent loop talks to ``LLMClient``. Everything provider-specific lives in a
provider class; everything cross-cutting (response caching, cost logging) lives
in wrappers around any client. Messages use plain Pydantic models so they can be
hashed, cached as JSON, and compared.

Two details that matter for correctness:

* **Opaque blocks.** Some providers return blocks the loop must not interpret
  but must send back unchanged on the next turn (for Claude, ``thinking``
  blocks). They are kept verbatim as ``OpaqueBlock`` and passed through.
* **No sampling parameters.** Current Claude models reject ``temperature`` /
  ``top_p`` / ``top_k``, so the request type does not have them.
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from agentgit_core.hashing import canonical_json

log = logging.getLogger("agentgit.agents")


# ---- messages and requests -------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


class OpaqueBlock(BaseModel):
    """A provider-specific block (e.g. thinking) that is stored and echoed back verbatim."""

    type: Literal["opaque"] = "opaque"
    data: dict[str, Any]


Block = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | OpaqueBlock, Field(discriminator="type")
]


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any]


class LLMRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    system: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    max_tokens: int = 16000
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    def cache_key(self) -> str:
        """Stable id of this exact request (same content, same key, any key order)."""
        return hashlib.sha256(canonical_json(self.model_dump(mode="json"))).hexdigest()


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


StopReason = Literal["end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "other"]


class LLMResponse(BaseModel):
    content: list[Block]
    stop_reason: StopReason
    usage: Usage = Field(default_factory=Usage)
    model: str = ""

    @property
    def text(self) -> str:
        return "".join(b.text for b in self.content if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> list[ToolUseBlock]:
        return [b for b in self.content if isinstance(b, ToolUseBlock)]


class LLMError(Exception):
    """The provider failed (network, auth, rate limit, bad request)."""


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


# ---- cost log ------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPrice:
    """Price per million tokens, in USD. Supplied by the user: prices change."""

    input: float
    output: float
    cache_read: float | None = None  # defaults to the input price
    cache_write: float | None = None  # defaults to the input price


Pricing = dict[str, ModelPrice]


@dataclass(frozen=True)
class CallRecord:
    model: str
    usage: Usage
    from_cache: bool
    duration_ms: int
    cost_usd: float | None  # None: no price known. 0.0: served from cache, nothing spent.


@dataclass
class CostLog:
    """Every model call an agent made, with an estimated cost when prices are known."""

    pricing: Pricing = field(default_factory=dict)
    calls: list[CallRecord] = field(default_factory=list)

    def record(self, response: LLMResponse, from_cache: bool, duration_ms: int) -> None:
        self.calls.append(
            CallRecord(
                model=response.model,
                usage=response.usage,
                from_cache=from_cache,
                duration_ms=duration_ms,
                cost_usd=self._cost(response, from_cache),
            )
        )

    def _cost(self, response: LLMResponse, from_cache: bool) -> float | None:
        if from_cache:
            return 0.0
        price = self.pricing.get(response.model)
        if price is None:
            return None
        u = response.usage
        return (
            u.input_tokens * price.input
            + u.output_tokens * price.output
            + u.cache_read_tokens
            * (price.cache_read if price.cache_read is not None else price.input)
            + u.cache_write_tokens
            * (price.cache_write if price.cache_write is not None else price.input)
        ) / 1_000_000

    @property
    def live_calls(self) -> int:
        return sum(1 for c in self.calls if not c.from_cache)

    @property
    def cached_calls(self) -> int:
        return sum(1 for c in self.calls if c.from_cache)

    @property
    def tokens_spent(self) -> Usage:
        """Tokens actually billed (cache hits cost nothing)."""
        total = Usage()
        for c in self.calls:
            if not c.from_cache:
                total.input_tokens += c.usage.input_tokens
                total.output_tokens += c.usage.output_tokens
                total.cache_read_tokens += c.usage.cache_read_tokens
                total.cache_write_tokens += c.usage.cache_write_tokens
        return total

    @property
    def estimated_cost_usd(self) -> float | None:
        """Sum of known costs; None if any live call had no known price."""
        costs = [c.cost_usd for c in self.calls]
        return None if any(c is None for c in costs) else sum(c for c in costs if c is not None)


# ---- response cache ------------------------------------------------------------


class ResponseCache(Protocol):
    def get(self, key: str) -> LLMResponse | None: ...
    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None: ...


class MemoryResponseCache:
    def __init__(self) -> None:
        self.entries: dict[str, tuple[LLMRequest, LLMResponse]] = {}

    def get(self, key: str) -> LLMResponse | None:
        entry = self.entries.get(key)
        return entry[1] if entry else None

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        self.entries[key] = (request, response)


class FileResponseCache:
    """One JSON file per call at ``<root>/<key[:2]>/<key[2:]>.json``.

    The request is stored next to the response so a cache entry can be read and
    diffed by a human.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, key: str) -> Path:
        return self._root / key[:2] / f"{key[2:]}.json"

    def get(self, key: str) -> LLMResponse | None:
        path = self._path(key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LLMResponse.model_validate(data["response"])
        except FileNotFoundError:
            return None
        except (ValueError, KeyError, OSError):
            log.warning("ignoring unreadable cache entry %s", path)
            return None

    def put(self, key: str, request: LLMRequest, response: LLMResponse) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "request": request.model_dump(mode="json"),
                "response": response.model_dump(mode="json"),
            },
            indent=2,
            ensure_ascii=False,
        )
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, path)  # atomic: never a half-written entry
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


CacheMode = Literal["off", "readwrite", "record", "replay"]


class CacheMiss(LLMError):
    """``replay`` mode needed a response that was never recorded."""


class CachingLLM:
    """Wraps any client with response caching and cost logging.

    Modes:
      off        always call the model; just log
      readwrite  return a cached response if there is one, else call and store
      record     always call the model and store (refresh the cache)
      replay     never call the model; a missing entry raises ``CacheMiss``

    ``replay`` is what makes a run reproducible without the network: the same
    requests get the same recorded model outputs.
    """

    def __init__(
        self,
        inner: LLMClient | None,
        cache: ResponseCache,
        mode: CacheMode = "readwrite",
        cost_log: CostLog | None = None,
    ) -> None:
        if inner is None and mode != "replay":
            raise ValueError("a model client is required unless the mode is 'replay'")
        self._inner = inner
        self._cache = cache
        self._mode = mode
        self.cost_log = cost_log or CostLog()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        key = request.cache_key()
        started = time.monotonic()
        if self._mode in ("readwrite", "replay"):
            cached = self._cache.get(key)
            if cached is not None:
                self.cost_log.record(cached, True, _ms_since(started))
                return cached
            if self._mode == "replay":
                raise CacheMiss(f"no recorded model response for request {key[:12]}")
        assert self._inner is not None
        response = await self._inner.complete(request)
        if self._mode != "off":
            self._cache.put(key, request, response)
        self.cost_log.record(response, False, _ms_since(started))
        return response


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# ---- scripted client (tests, offline demos) -------------------------------------


class ScriptedLLM:
    """Returns pre-written responses in order. Deterministic; needs no network."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.requests: list[LLMRequest] = []

    @classmethod
    def from_file(cls, path: Path) -> "ScriptedLLM":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls([LLMResponse.model_validate(r) for r in raw])

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if not self._responses:
            raise LLMError("the script ran out of responses")
        return self._responses.pop(0)


def text_response(text: str, stop: StopReason = "end_turn") -> LLMResponse:
    return LLMResponse(content=[TextBlock(text=text)], stop_reason=stop, model="scripted")


def tool_response(*calls: tuple[str, str, dict[str, Any]], text: str = "") -> LLMResponse:
    """A response asking for tools: each call is ``(id, name, arguments)``."""
    blocks: list[Block] = [TextBlock(text=text)] if text else []
    blocks += [ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls]
    return LLMResponse(content=blocks, stop_reason="tool_use", model="scripted")


# ---- Claude provider -------------------------------------------------------------

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicLLM:
    """Claude through the official ``anthropic`` SDK.

    Credentials come from the environment (``ANTHROPIC_API_KEY`` or a logged-in
    profile); nothing is read from or written to disk by this class.

    Notes for these models: thinking is left at its default (it cannot be
    disabled on the newest models), tools use ``tool_choice: auto`` only (forced
    tool use is rejected), and no sampling parameters are sent. Assistant turns
    are replayed with their thinking blocks unchanged, via ``OpaqueBlock``.
    """

    def __init__(
        self, client: Any | None = None, *, timeout: float = 120.0, max_retries: int = 2
    ) -> None:
        if client is None:
            import anthropic

            client = anthropic.AsyncAnthropic(timeout=timeout, max_retries=max_retries)
        self._client = client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        import anthropic

        try:
            message = await self._client.messages.create(**self._kwargs(request))
        except anthropic.APIError as exc:
            raise LLMError(f"{type(exc).__name__}: {exc}") from exc
        return self._parse(message)

    @staticmethod
    def _kwargs(request: LLMRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            "messages": [_to_api_message(m) for m in request.messages],
        }
        if request.system:
            kwargs["system"] = request.system
        if request.tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema}
                for t in request.tools
            ]
        if request.effort:
            kwargs["output_config"] = {"effort": request.effort}
        return kwargs

    @staticmethod
    def _parse(message: Any) -> LLMResponse:
        blocks: list[Block] = []
        for block in message.content:
            if block.type == "text":
                blocks.append(TextBlock(text=block.text))
            elif block.type == "tool_use":
                blocks.append(ToolUseBlock(id=block.id, name=block.name, input=dict(block.input)))
            else:  # thinking and anything else: keep verbatim so it can be echoed back
                blocks.append(OpaqueBlock(data=block.model_dump(mode="json", exclude_none=True)))
        reason = message.stop_reason
        known = ("end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal")
        usage = message.usage
        return LLMResponse(
            content=blocks,
            stop_reason=reason if reason in known else "other",
            usage=Usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            ),
            model=message.model,
        )


def _to_api_message(message: Message) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            content.append({"type": "text", "text": block.text})
        elif isinstance(block, ToolUseBlock):
            content.append(
                {"type": "tool_use", "id": block.id, "name": block.name, "input": block.input}
            )
        elif isinstance(block, ToolResultBlock):
            content.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.tool_use_id,
                    "content": block.content,
                    "is_error": block.is_error,
                }
            )
        else:
            content.append(block.data)
    return {"role": message.role, "content": content}
