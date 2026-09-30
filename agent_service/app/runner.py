"""Runs one agent: builds the prompt, calls the LLM, validates output, retries once on
malformed JSON or (text agents) an empty reply."""

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .agents import LoadedAgent
from .llm import LLM, LLMError
from .schemas import Usage

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # first try + one retry (bad JSON for JSON agents, empty reply for text agents)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


class AgentOutputError(LLMError):
    """The model answered, but not in the contract the agent requires."""

    def __init__(self, message: str):
        super().__init__(message, status_code=502)


@dataclass
class RunResult:
    output: Any
    model: str
    usage: Usage
    attempts: int
    latency_ms: int


def serialize_input(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, indent=2, ensure_ascii=False, default=str)


def extract_json(text: str) -> Any:
    """Parse a JSON object out of model text, tolerating ``` fences and stray prose."""
    cleaned = _FENCE_RE.sub("", text.strip()).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start != -1 and end > start:
            return json.loads(cleaned[start : end + 1])
        raise


def _error_summary(exc: Exception) -> str:
    """Why an output was rejected, without echoing any prompt or response content."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(x) for x in e['loc']) or '<root>'}: {e['type']}" for e in exc.errors()
        )
    if isinstance(exc, json.JSONDecodeError):
        return f"JSON parse error at char {exc.pos}: {exc.msg}"
    return type(exc).__name__


async def run_agent(
    agent: LoadedAgent,
    payload: Any,
    llm: LLM,
    *,
    default_max_tokens: int,
) -> RunResult:
    spec = agent.spec
    assert agent.instructions is not None
    user_text = spec.input_prefix + serialize_input(payload)
    max_tokens = spec.max_tokens or default_max_tokens

    messages: list[dict] = [{"role": "user", "content": user_text}]
    in_tok = out_tok = 0
    started = time.perf_counter()
    model = llm.model
    log.info("Running agent=%s input_chars=%d", spec.name, len(user_text))
    log.debug("Agent %s input: %s", spec.name, user_text)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result = await llm.complete(
            system=agent.instructions,
            messages=messages,
            max_tokens=max_tokens,
        )
        in_tok += result.input_tokens
        out_tok += result.output_tokens
        model = result.model
        log.debug("Agent %s attempt %d raw output: %s", spec.name, attempt, result.text)

        if spec.output_kind == "text":
            text = result.text.strip()
            if not text:
                # Seen in live runs: now and then the model returns no text at all. Try once more.
                # Nothing from the prompt or reply is logged, only why the reply was empty.
                cut_off = result.stop_reason == "max_tokens"
                log.warning(
                    "Agent %s attempt %d/%d returned an empty response (stop_reason=%s, out_tokens=%d)%s",
                    spec.name,
                    attempt,
                    MAX_ATTEMPTS,
                    result.stop_reason,
                    result.output_tokens,
                    " - likely cut off before any visible text; raise this agent's max_tokens"
                    if cut_off
                    else "",
                )
                if attempt == MAX_ATTEMPTS:
                    message = (
                        "The model's reply was cut off at max_tokens before any visible text."
                        if cut_off
                        else "The model returned an empty response."
                    )
                    raise AgentOutputError(message)
                continue
            output: Any = text
            break

        # JSON agent: parse, then validate against the schema.
        try:
            parsed = extract_json(result.text)
            assert spec.output_schema is not None
            output = spec.output_schema.model_validate(parsed).model_dump()
            break
        except (json.JSONDecodeError, ValidationError) as exc:
            truncated = result.stop_reason == "max_tokens"
            problem = "output was cut off at max_tokens" if truncated else str(exc)
            log.warning(
                "Agent %s attempt %d/%d produced invalid JSON output: %s (stop_reason=%s, out_tokens=%d)",
                spec.name,
                attempt,
                MAX_ATTEMPTS,
                _error_summary(exc),
                result.stop_reason,
                result.output_tokens,
            )
            if attempt == MAX_ATTEMPTS:
                raise AgentOutputError(
                    f"Agent '{spec.name}' did not return valid output after {attempt} attempts."
                ) from exc
            keys = ", ".join(spec.output_schema.model_fields)
            messages = messages + [
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply could not be used: "
                        f"{problem[:500]}\n"
                        "Reply again with ONLY the corrected JSON object. Use exactly these "
                        f"snake_case keys: {keys}. No code fences, no commentary."
                    ),
                },
            ]

    latency_ms = int((time.perf_counter() - started) * 1000)
    log.info(
        "Agent %s done attempts=%d in_tokens=%d out_tokens=%d latency_ms=%d",
        spec.name,
        attempt,
        in_tok,
        out_tok,
        latency_ms,
    )
    return RunResult(
        output=output,
        model=model,
        usage=Usage(input_tokens=in_tok, output_tokens=out_tok),
        attempts=attempt,
        latency_ms=latency_ms,
    )
