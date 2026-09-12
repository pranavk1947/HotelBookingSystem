"""Anthropic adapter behind a Protocol the agent service owns."""

from __future__ import annotations

from typing import Any
from typing import Protocol


class LLMError(Exception):
    """An upstream failure worth showing the user in plain words."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class LLMClient(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class AnthropicLLMClient:
    def __init__(self, api_key: str, model: str) -> None:
        from anthropic import Anthropic

        self._client = Anthropic(api_key=api_key)
        self.model = model

    def create(self, **kwargs: Any) -> Any:
        import anthropic

        try:
            return self._client.messages.create(model=self.model, **kwargs)
        except anthropic.AuthenticationError as exc:
            raise LLMError(
                "invalid_api_key",
                "Anthropic rejected the API key. Check ANTHROPIC_API_KEY in .env.",
            ) from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(
                "unknown_model",
                f"The model '{self.model}' is not available on this API key. "
                "Set ANTHROPIC_MODEL in .env to one your account can use.",
            ) from exc
        except anthropic.RateLimitError as exc:
            raise LLMError(
                "rate_limited",
                "Anthropic is rate limiting this key. Wait a moment and resend.",
            ) from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(
                "upstream_error", f"Anthropic returned {exc.status_code}."
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(
                "connection_error", "Could not reach the Anthropic API."
            ) from exc
