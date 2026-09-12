"""Agent-loop tests.

The real model needs an API key, so these drive AgentService through a scripted
LLM stub. That is enough to pin the parts that are ours rather than the model's:
tool dispatch, history hygiene, the round cap, and the rule that a turn never
ends without something the customer can read.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.agent.agent_service import AgentService
from src.agent.llm_client import LLMError
from src.agent.prompt_builder import SystemPromptBuilder
from src.config.settings import Settings
from src.model.chat import ChatMessage
from src.model.hotel_config import HotelConfig

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"
TODAY = date(2026, 1, 15)


@pytest.fixture
def config() -> HotelConfig:
    return HotelConfig.model_validate(
        json.loads((CONFIG_DIR / "grand-cascadia.json").read_text())
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test-key", max_tool_rounds=3)


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_block(payload: dict, block_id: str = "tu_1") -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=block_id, name="build_quote", input=payload
    )


def response(content: list, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=100, output_tokens=50),
    )


class StubLLM:
    """Replays a scripted list of responses and records what it was sent."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        # Snapshot: the service appends to one messages list across rounds, so
        # storing it by reference would make every recorded call identical.
        self.calls.append({**kwargs, "messages": copy.deepcopy(kwargs["messages"])})
        if not self.script:
            return response([text_block("(out of script)")])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def service(llm, settings: Settings) -> AgentService:
    return AgentService(llm, SystemPromptBuilder(), settings)


BRIEF_SELECTION = {
    "arrival_date": "2026-03-09",
    "nights": 3,
    "attendees": 40,
    "selections": [
        {"item_id": "std-room", "qty": 40},
        {"item_id": "grand-ballroom", "qty": 1},
        {"item_id": "plated-dinner", "qty": 40},
    ],
}


def test_tool_result_is_priced_by_the_engine_and_returned_to_the_client(
    config: HotelConfig, settings: Settings
) -> None:
    llm = StubLLM(
        [
            response([tool_block(BRIEF_SELECTION)], stop_reason="tool_use"),
            response([text_block("Here is the proposal.")]),
        ]
    )
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="3-day conference, 40 people")], TODAY
    )

    assert result.error is None
    assert result.reply == "Here is the proposal."
    assert result.quote is not None
    assert result.quote.subtotal == 32_880.00        # 22,680 + 7,200 + 3,000
    assert result.quote.adjustments[0].amount == -2_268.00
    assert result.usage.rounds == 2

    # The engine's numbers, not the model's, went back into the conversation.
    tool_turn = llm.calls[1]["messages"][-1]["content"][0]
    payload = json.loads(tool_turn["content"])
    assert payload["net_subtotal"] == 30_612.00


def test_violations_come_back_as_a_normal_result_so_the_model_can_retry(
    config: HotelConfig, settings: Settings
) -> None:
    too_small = {
        "arrival_date": "2026-03-09",
        "nights": 1,
        "attendees": 200,
        "selections": [{"item_id": "breakout-a", "qty": 1}],
    }
    llm = StubLLM(
        [
            response([tool_block(too_small)], stop_reason="tool_use"),
            response(
                [tool_block({**too_small, "selections": [{"item_id": "grand-ballroom", "qty": 1}]}, "tu_2")],
                stop_reason="tool_use",
            ),
            response([text_block("I've moved you into the Ballroom.")]),
        ]
    )
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="200 people, one day")], TODAY
    )

    first_result = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert first_result["violations"][0]["code"] == "capacity_exceeded"
    # Not an error block: an error makes the model apologise, a result makes it fix.
    assert "is_error" not in llm.calls[1]["messages"][-1]["content"][0]
    assert result.quote is not None
    assert result.quote.lines[0].item_id == "grand-ballroom"


def test_unknown_item_id_is_answered_with_a_suggestion(
    config: HotelConfig, settings: Settings
) -> None:
    llm = StubLLM(
        [
            response(
                [tool_block({**BRIEF_SELECTION, "selections": [{"item_id": "ballroom", "qty": 1}]})],
                stop_reason="tool_use",
            ),
            response([text_block("Fixed.")]),
        ]
    )
    service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="book the ballroom")], TODAY
    )
    payload = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert payload["violations"][0]["suggestion"] == "Did you mean 'grand-ballroom'?"


def test_history_is_stripped_of_stale_tool_blocks(
    config: HotelConfig, settings: Settings
) -> None:
    """A prior quote must not be replayed after the config changes under us."""
    llm = StubLLM([response([text_block("ok")])])
    history = [
        ChatMessage(role="user", content="40 people in March"),
        ChatMessage(role="assistant", content="| Line | Qty |\n| Rooms | 40 |"),
        ChatMessage(role="user", content="what about 50?"),
    ]
    service(llm, settings).run_turn(config, history, TODAY)

    sent = llm.calls[0]["messages"]
    assert all(isinstance(message["content"], str) for message in sent)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert "always re-call" not in llm.calls[0]["system"]  # instruction is phrased once
    assert "may be stale" in llm.calls[0]["system"]


def test_consecutive_same_role_messages_are_merged(
    config: HotelConfig, settings: Settings
) -> None:
    """The API rejects two user turns in a row; a double-send must not 400."""
    llm = StubLLM([response([text_block("ok")])])
    history = [
        ChatMessage(role="user", content="40 people"),
        ChatMessage(role="user", content="in March"),
    ]
    service(llm, settings).run_turn(config, history, TODAY)
    sent = llm.calls[0]["messages"]
    assert len(sent) == 1
    assert sent[0]["content"] == "40 people\n\nin March"


def test_history_is_capped(config: HotelConfig, settings: Settings) -> None:
    settings.max_history_messages = 4
    llm = StubLLM([response([text_block("ok")])])
    history = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant", content=f"turn {i}")
        for i in range(20)
    ]
    service(llm, settings).run_turn(config, history, TODAY)
    assert len(llm.calls[0]["messages"]) == 4


def test_round_cap_closes_the_turn_without_free_styling_prices(
    config: HotelConfig, settings: Settings
) -> None:
    """Out of rounds, the model gets one no-tools turn — then we fall back."""
    settings.max_tool_rounds = 2
    llm = StubLLM(
        [
            response([tool_block(BRIEF_SELECTION, "tu_1")], stop_reason="tool_use"),
            response([tool_block(BRIEF_SELECTION, "tu_2")], stop_reason="tool_use"),
            response([text_block("Final answer from the capped turn.")]),
        ]
    )
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="quote me")], TODAY
    )

    assert result.reply == "Final answer from the capped turn."
    assert llm.calls[-1]["tool_choice"] == {"type": "none"}
    assert result.quote is not None


def test_falls_back_to_the_last_priced_table_if_the_final_call_fails(
    config: HotelConfig, settings: Settings
) -> None:
    settings.max_tool_rounds = 1
    llm = StubLLM(
        [
            response([tool_block(BRIEF_SELECTION)], stop_reason="tool_use"),
            LLMError("upstream_error", "Anthropic returned 500."),
        ]
    )
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="quote me")], TODAY
    )

    # Never a dead turn, and never a number the engine did not produce.
    assert "| Line | Qty | Unit price | Duration | Subtotal |" in result.reply
    assert "$32,880.00" in result.reply


def test_upstream_failure_is_reported_in_plain_words(
    config: HotelConfig, settings: Settings
) -> None:
    llm = StubLLM([LLMError("invalid_api_key", "Anthropic rejected the API key.")])
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="hi")], TODAY
    )
    assert result.error == "invalid_api_key"
    assert "rejected the API key" in result.reply
    assert result.quote is None


def test_missing_api_key_is_a_readable_turn_not_a_crash(
    config: HotelConfig, settings: Settings
) -> None:
    result = AgentService(None, SystemPromptBuilder(), settings).run_turn(
        config, [ChatMessage(role="user", content="hi")], TODAY
    )
    assert result.error == "missing_api_key"
    assert "ANTHROPIC_API_KEY" in result.reply


def test_impossible_date_is_a_violation_not_a_crash(
    config: HotelConfig, settings: Settings
) -> None:
    """30 February passes the regex but not the calendar; the turn must survive."""
    bad = {**BRIEF_SELECTION, "arrival_date": "2027-02-30"}
    llm = StubLLM(
        [
            response([tool_block(bad)], stop_reason="tool_use"),
            response([tool_block(BRIEF_SELECTION, "tu_2")], stop_reason="tool_use"),
            response([text_block("Corrected the date.")]),
        ]
    )
    result = service(llm, settings).run_turn(
        config, [ChatMessage(role="user", content="book us in February")], TODAY
    )

    first = json.loads(llm.calls[1]["messages"][-1]["content"][0]["content"])
    assert first["violations"][0]["code"] == "bad_tool_input"
    assert "real calendar date" in first["violations"][0]["message"]
    assert result.error is None
    assert result.quote is not None


def test_an_unexpected_failure_ends_the_turn_readably(
    config: HotelConfig, settings: Settings
) -> None:
    """Anything unforeseen must reach the browser as prose, never as a 500."""

    class Exploding:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    result = service(Exploding(), settings).run_turn(
        config, [ChatMessage(role="user", content="hi")], TODAY
    )
    assert result.error == "internal"
    assert "boom" not in result.reply
    assert "Something went wrong" in result.reply
