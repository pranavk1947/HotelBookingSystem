"""OpenAI adapter that speaks the Anthropic message shape.

The agent service is written against one vendor's shape; rather than teach it a
second one, this client translates both ways at the boundary. Everything above
it — the tool loop, the prompt builder, the pricing engine — is untouched.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from src.agent.llm_client import LLMError

logger = logging.getLogger(__name__)


# --- Anthropic-shaped response objects the agent service reads ----------


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Response:
    content: list = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)


# --- translation --------------------------------------------------------


def to_openai_tools(tools: list[dict] | None) -> list[dict] | None:
    """Anthropic ``input_schema`` -> OpenAI ``function.parameters``."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description", ""),
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


def to_openai_messages(system: str | None, messages: list[dict]) -> list[dict]:
    """Flatten Anthropic content blocks into OpenAI's role-per-message shape.

    Anthropic carries tool calls and their results as content blocks inside
    assistant/user turns; OpenAI puts them in ``tool_calls`` and in separate
    ``role: "tool"`` messages.
    """
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})

    for message in messages:
        role = message["role"]
        content = message["content"]

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            out.append(_assistant_message(content))
            continue

        # A user turn carrying tool results becomes one tool message per result.
        results = [block for block in content if _block_type(block) == "tool_result"]
        if results:
            out.extend(_tool_messages(results))
            continue

        text = _joined_text(content)
        out.append({"role": role, "content": text or ""})

    return out


def _assistant_message(blocks: list) -> dict:
    text = _joined_text(blocks)
    tool_calls = [
        {
            "id": _get(block, "id"),
            "type": "function",
            "function": {
                "name": _get(block, "name"),
                "arguments": json.dumps(_get(block, "input") or {}),
            },
        }
        for block in blocks
        if _block_type(block) == "tool_use"
    ]
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _tool_messages(results: list) -> list[dict]:
    messages = []
    for block in results:
        content = _get(block, "content")
        if not isinstance(content, str):
            content = json.dumps(content)
        messages.append(
            {
                "role": "tool",
                "tool_call_id": _get(block, "tool_use_id"),
                "content": content,
            }
        )
    return messages


def _joined_text(blocks: list) -> str:
    return "\n\n".join(
        _get(block, "text") or ""
        for block in blocks
        if _block_type(block) == "text"
    ).strip()


def _block_type(block: Any) -> str:
    return _get(block, "type") or ""


def _get(block: Any, key: str) -> Any:
    """Blocks arrive as dicts from us and as SDK objects from a response."""
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def from_openai_response(completion: Any) -> Response:
    """OpenAI completion -> the Anthropic-shaped object the service expects."""
    choice = completion.choices[0]
    message = choice.message
    usage = Usage(
        input_tokens=getattr(completion.usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(completion.usage, "completion_tokens", 0) or 0,
    )

    tool_calls = getattr(message, "tool_calls", None) or []
    if choice.finish_reason == "tool_calls" and tool_calls:
        content: list = []
        if message.content:
            content.append(TextBlock(text=message.content))
        for call in tool_calls:
            content.append(
                ToolUseBlock(
                    id=call.id,
                    name=call.function.name,
                    input=_parse_arguments(call.function.arguments, call.function.name),
                )
            )
        return Response(content=content, stop_reason="tool_use", usage=usage)

    return Response(
        content=[TextBlock(text=message.content or "")],
        stop_reason="end_turn",
        usage=usage,
    )


def _parse_arguments(arguments: str, tool_name: str) -> dict:
    """Malformed arguments become an empty call, which the tool answers with a
    violation the model can correct from — better than dropping the turn."""
    try:
        parsed = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        logger.warning("Unparseable tool arguments from %s", tool_name)
        return {}
    return parsed if isinstance(parsed, dict) else {}


# --- the client ---------------------------------------------------------


class OpenAILLMClient:
    """Same ``create(**kwargs)`` contract as AnthropicLLMClient."""

    def __init__(self, api_key: str, model: str) -> None:
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model = model

    def create(self, **kwargs: Any) -> Response:
        import openai

        request: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(kwargs.get("system"), kwargs["messages"]),
        }
        if kwargs.get("max_tokens") is not None:
            request["max_completion_tokens"] = kwargs["max_tokens"]
        if kwargs.get("temperature") is not None:
            request["temperature"] = kwargs["temperature"]

        tools = to_openai_tools(kwargs.get("tools"))
        if tools:
            request["tools"] = tools
            choice = kwargs.get("tool_choice")
            request["tool_choice"] = (
                "none" if isinstance(choice, dict) and choice.get("type") == "none"
                else "auto"
            )

        try:
            completion = self._client.chat.completions.create(**request)
        except openai.AuthenticationError as exc:
            raise LLMError(
                "invalid_api_key",
                "OpenAI rejected the API key. Check OPENAI_API_KEY in .env.",
            ) from exc
        except openai.NotFoundError as exc:
            raise LLMError(
                "unknown_model",
                f"The model '{self.model}' is not available on this API key. "
                "Set OPENAI_MODEL in .env to one your account can use.",
            ) from exc
        except openai.RateLimitError as exc:
            raise LLMError(
                "rate_limited",
                "OpenAI is rate limiting this key. Wait a moment and resend.",
            ) from exc
        except openai.APIStatusError as exc:
            raise LLMError(
                "upstream_error", f"OpenAI returned {exc.status_code}."
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMError(
                "connection_error", "Could not reach the OpenAI API."
            ) from exc

        return from_openai_response(completion)
