"""Thin wrapper around the Anthropic Messages API so the rest of the app (and the tests)
never touch the SDK directly."""

import logging
from dataclasses import dataclass
from typing import Protocol

import anthropic

log = logging.getLogger(__name__)


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str
    stop_reason: str | None


class LLMError(Exception):
    """Raised for upstream LLM failures. `status_code` is what this service returns."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


class LLM(Protocol):
    model: str

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
    ) -> LLMResult: ...


class AnthropicLLM:
    def __init__(
        self,
        api_key: str,
        model: str,
        timeout: float,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self.model = model
        # max_retries handles transient 429/5xx/connection errors with backoff.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout, max_retries=2
        )

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict],
        max_tokens: int,
    ) -> LLMResult:
        # Note: anthropic SDK 1.x's messages.create() no longer accepts sampling
        # parameters such as temperature, so none are passed.
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except anthropic.APITimeoutError as exc:
            log.error("Anthropic request timed out")
            raise LLMError("The LLM request timed out.", 504) from exc
        except anthropic.APIStatusError as exc:
            log.error("Anthropic API error: status=%s", exc.status_code)
            raise LLMError(f"LLM provider returned an error (status {exc.status_code}).", 502) from exc
        except anthropic.APIConnectionError as exc:
            log.error("Could not connect to Anthropic API: %s", exc)
            raise LLMError("Could not reach the LLM provider.", 502) from exc

        text = "".join(block.text for block in resp.content if block.type == "text")
        return LLMResult(
            text=text,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            model=resp.model,
            stop_reason=resp.stop_reason,
        )
