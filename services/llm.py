"""
Thin wrapper around the Anthropic SDK.

Centralises: client construction, the model id, the "retry once with exponential
backoff" policy from the spec, and a streaming helper for chat.
"""
import asyncio
from typing import AsyncIterator, Optional

from anthropic import AsyncAnthropic, APIError, APIStatusError

import config

_client: Optional[AsyncAnthropic] = None


def get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Configure it in the environment."
            )
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def complete(
    system: str,
    user_content: str,
    *,
    max_tokens: int = config.MAX_TOKENS_EXTRACTION,
) -> str:
    """
    Single-turn completion with one retry on transient failure.
    Returns the concatenated text of the response.
    """
    client = get_client()
    attempt = 0
    last_exc: Exception | None = None
    while attempt <= config.API_MAX_RETRIES:
        try:
            resp = await client.messages.create(
                model=config.ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_content}],
            )
            return "".join(block.text for block in resp.content if block.type == "text")
        except (APIError, APIStatusError) as exc:
            last_exc = exc
            if attempt >= config.API_MAX_RETRIES:
                break
            await asyncio.sleep(config.API_RETRY_BASE_DELAY * (2 ** attempt))
            attempt += 1
    raise RuntimeError(f"Anthropic API call failed after retries: {last_exc}")


async def stream_chat(
    system: str,
    messages: list[dict],
    *,
    max_tokens: int = config.MAX_TOKENS_DRAFTING,
) -> AsyncIterator[str]:
    """
    Async generator yielding text deltas for a multi-turn chat.
    Retries once on connection failure BEFORE the first token is emitted.
    """
    client = get_client()
    attempt = 0
    while True:
        try:
            async with client.messages.stream(
                model=config.ANTHROPIC_MODEL,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
            return
        except (APIError, APIStatusError) as exc:
            if attempt >= config.API_MAX_RETRIES:
                raise RuntimeError(f"Anthropic streaming failed after retries: {exc}") from exc
            await asyncio.sleep(config.API_RETRY_BASE_DELAY * (2 ** attempt))
            attempt += 1
