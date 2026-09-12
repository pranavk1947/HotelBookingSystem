"""The agent turn: a facade over history hygiene, the tool loop and failure.

Two rules it enforces: the model reaches a price only through ``build_quote``,
and a turn always ends in something the customer can read.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from src.agent.llm_client import LLMClient
from src.agent.llm_client import LLMError
from src.agent.prompt_builder import SystemPromptBuilder
from src.agent.tools import BuildQuoteTool
from src.agent.tools import ToolRegistry
from src.config.settings import Settings
from src.model.chat import ChatMessage
from src.model.chat import ChatResponse
from src.model.chat import Usage
from src.model.hotel_config import HotelConfig

logger = logging.getLogger(__name__)

SETUP_MESSAGE = (
    "Setup needed: ANTHROPIC_API_KEY is not set, so I can't run the agent yet. "
    "Add it to the .env file in the project root and restart. "
    "The config page works without a key."
)


class AgentService:
    def __init__(
        self,
        llm: LLMClient | None,
        prompt_builder: SystemPromptBuilder,
        settings: Settings,
    ) -> None:
        self.llm = llm
        self.prompt_builder = prompt_builder
        self.settings = settings

    def run_turn(
        self,
        config: HotelConfig,
        history: list[ChatMessage],
        today: date | None = None,
    ) -> ChatResponse:
        if self.llm is None:
            return ChatResponse(reply=SETUP_MESSAGE, error="missing_api_key")

        today = today or date.today()
        tool = BuildQuoteTool(config, today=today)
        registry = ToolRegistry([tool])
        system = self.prompt_builder.build(config, today)
        messages = self._sanitise(history)

        usage = Usage()
        try:
            reply = self._loop(system, messages, registry, tool, usage)
        except LLMError as exc:
            logger.warning("LLM call failed: %s", exc.message)
            return ChatResponse(reply=exc.message, error=exc.code, usage=usage)
        except Exception:  # noqa: BLE001 - a turn must never 500 the chat page
            logger.exception("Agent turn failed for config %s", config.id)
            return ChatResponse(
                reply=(
                    "Something went wrong pricing that. Tell me the dates and "
                    "headcount again and I'll redo it."
                ),
                error="internal",
                usage=usage,
            )

        return ChatResponse(reply=reply, quote=tool.last_quote, usage=usage)

    # --- internals ------------------------------------------------------

    def _sanitise(self, history: list[ChatMessage]) -> list[dict]:
        """Replay text only, most recent N messages.

        Prior tool blocks are dropped: replaying an old quote invites the model to
        restate prices that a config edit has since changed.
        """
        recent = history[-self.settings.max_history_messages :]
        messages: list[dict] = []
        for message in recent:
            text = message.content.strip()
            if not text:
                continue
            # The API rejects consecutive same-role turns; merge instead.
            if messages and messages[-1]["role"] == message.role:
                messages[-1]["content"] += "\n\n" + text
                continue
            messages.append({"role": message.role, "content": text})
        if not messages or messages[0]["role"] != "user":
            messages.insert(0, {"role": "user", "content": "Hello."})
        return messages

    def _loop(
        self,
        system: str,
        messages: list[dict],
        registry: ToolRegistry,
        tool: BuildQuoteTool,
        usage: Usage,
    ) -> str:
        tools = registry.anthropic_tools()

        for _ in range(self.settings.max_tool_rounds):
            response = self.llm.create(
                system=system,
                messages=messages,
                tools=tools,
                max_tokens=self.settings.max_tokens,
            )
            usage.rounds += 1
            self._record_usage(usage, response)

            if response.stop_reason != "tool_use":
                return self._text_of(response) or self._fallback(tool)

            messages.append({"role": "assistant", "content": response.content})
            messages.append(
                {"role": "user", "content": self._run_tools(response, registry)}
            )

        return self._finish_on_cap(system, messages, tools, tool, usage)

    def _run_tools(self, response, registry: ToolRegistry) -> list[dict]:
        results: list[dict] = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            payload = registry.dispatch(block.name, dict(block.input))
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    # Not is_error: an error block makes the model apologise, a
                    # normal result makes it re-select and fix.
                    "content": json.dumps(payload, ensure_ascii=False),
                }
            )
        return results

    def _finish_on_cap(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        tool: BuildQuoteTool,
        usage: Usage,
    ) -> str:
        """Out of rounds: end the turn without letting the model free-style prices."""
        logger.info("Tool loop hit the round cap; closing the turn")
        messages.append(
            {
                "role": "user",
                "content": (
                    "Write your reply to the customer now, using only figures from "
                    "the most recent build_quote result. Do not call any more tools."
                ),
            }
        )
        try:
            response = self.llm.create(
                system=system,
                messages=messages,
                tools=tools,
                tool_choice={"type": "none"},
                max_tokens=self.settings.max_tokens,
            )
            usage.rounds += 1
            self._record_usage(usage, response)
            text = self._text_of(response)
            if text:
                return text
        except LLMError:
            pass
        return self._fallback(tool)

    def _fallback(self, tool: BuildQuoteTool) -> str:
        """Last resort: the last priced quote verbatim, or an honest nudge."""
        if tool.last_quote is not None:
            return (
                "Here is where the proposal stands:\n\n"
                f"{tool.last_quote.markdown_table}\n\n"
                "Tell me what to change and I'll re-price it."
            )
        return (
            "I have your requirements but couldn't finish pricing them just now. "
            "Send the dates, head count and what you need during the day, and "
            "I'll put the numbers together."
        )

    @staticmethod
    def _text_of(response) -> str:
        return "\n\n".join(
            block.text for block in response.content if block.type == "text"
        ).strip()

    @staticmethod
    def _record_usage(usage: Usage, response) -> None:
        if getattr(response, "usage", None) is None:
            return
        usage.input_tokens += response.usage.input_tokens
        usage.output_tokens += response.usage.output_tokens
