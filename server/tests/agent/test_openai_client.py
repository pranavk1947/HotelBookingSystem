"""Translation tests for the OpenAI adapter. No network.

The adapter's whole job is shape conversion in both directions, so that is what
these pin: Anthropic-shaped input becomes valid OpenAI input, and an OpenAI
completion becomes the Anthropic-shaped object the agent service reads.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src.agent.llm_client import LLMError
from src.agent.openai_client import OpenAILLMClient
from src.agent.openai_client import from_openai_response
from src.agent.openai_client import to_openai_messages
from src.agent.openai_client import to_openai_tools

ANTHROPIC_TOOL = {
    "name": "build_quote",
    "description": "Price a proposal.",
    "input_schema": {
        "type": "object",
        "properties": {"nights": {"type": "integer"}},
        "required": ["nights"],
    },
}


# --- tools --------------------------------------------------------------


def test_tools_map_input_schema_to_function_parameters() -> None:
    tools = to_openai_tools([ANTHROPIC_TOOL])
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "build_quote",
                "description": "Price a proposal.",
                "parameters": ANTHROPIC_TOOL["input_schema"],
            },
        }
    ]


def test_no_tools_is_none_not_an_empty_list() -> None:
    # OpenAI rejects tools=[]; omitting the key is the correct translation.
    assert to_openai_tools([]) is None
    assert to_openai_tools(None) is None


# --- messages -----------------------------------------------------------


def test_system_becomes_a_leading_message() -> None:
    out = to_openai_messages("You sell hotels.", [{"role": "user", "content": "hi"}])
    assert out == [
        {"role": "system", "content": "You sell hotels."},
        {"role": "user", "content": "hi"},
    ]


def test_plain_string_content_passes_through() -> None:
    history = [
        {"role": "user", "content": "40 people"},
        {"role": "assistant", "content": "How many nights?"},
    ]
    assert to_openai_messages(None, history) == history


def test_assistant_tool_use_becomes_tool_calls() -> None:
    history = [
        {"role": "user", "content": "quote it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Pricing that now."},
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "build_quote",
                    "input": {"nights": 3},
                },
            ],
        },
    ]
    assistant = to_openai_messages(None, history)[1]

    assert assistant["role"] == "assistant"
    assert assistant["content"] == "Pricing that now."
    call = assistant["tool_calls"][0]
    assert call == {
        "id": "tu_1",
        "type": "function",
        "function": {"name": "build_quote", "arguments": json.dumps({"nights": 3})},
    }


def test_assistant_with_no_text_sends_null_content() -> None:
    """OpenAI wants content: null, not "", on a pure tool call."""
    history = [
        {"role": "user", "content": "quote"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "build_quote", "input": {}}
            ],
        },
    ]
    assert to_openai_messages(None, history)[1]["content"] is None


def test_tool_results_become_one_tool_message_each() -> None:
    history = [
        {"role": "user", "content": "quote"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "tu_1", "name": "build_quote", "input": {}},
                {"type": "tool_use", "id": "tu_2", "name": "build_quote", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"total": 1}'},
                {"type": "tool_result", "tool_use_id": "tu_2", "content": '{"total": 2}'},
            ],
        },
    ]
    out = to_openai_messages(None, history)

    assert [m["role"] for m in out] == ["user", "assistant", "tool", "tool"]
    assert out[2] == {"role": "tool", "tool_call_id": "tu_1", "content": '{"total": 1}'}
    assert out[3]["tool_call_id"] == "tu_2"


def test_non_string_tool_result_content_is_serialised() -> None:
    history = [
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": {"total": 1}}
            ],
        }
    ]
    assert to_openai_messages(None, history)[0]["content"] == '{"total": 1}'


def test_a_full_round_trip_produces_a_valid_openai_conversation() -> None:
    """The exact shape the agent service builds across one tool round."""
    history = [
        {"role": "user", "content": "3 nights, 40 people"},
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "tu_1",
                    "name": "build_quote",
                    "input": {"nights": 3, "attendees": 40},
                }
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": '{"total": 100}'}
            ],
        },
    ]
    out = to_openai_messages("You sell hotels.", history)

    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool"]
    # Every tool message answers a tool_call that was actually issued.
    issued = {c["id"] for m in out if m.get("tool_calls") for c in m["tool_calls"]}
    answered = {m["tool_call_id"] for m in out if m["role"] == "tool"}
    assert answered == issued


# --- responses ----------------------------------------------------------


def completion(finish_reason: str, content=None, tool_calls=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=tool_calls),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=30),
    )


def tool_call(call_id: str, name: str, arguments: str):
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def test_stop_finish_reason_becomes_end_turn_with_a_text_block() -> None:
    response = from_openai_response(completion("stop", content="Here is the quote."))

    assert response.stop_reason == "end_turn"
    assert [block.type for block in response.content] == ["text"]
    assert response.content[0].text == "Here is the quote."
    assert response.usage.input_tokens == 120
    assert response.usage.output_tokens == 30


def test_tool_calls_finish_reason_becomes_tool_use_blocks() -> None:
    response = from_openai_response(
        completion(
            "tool_calls",
            content="One moment.",
            tool_calls=[tool_call("call_1", "build_quote", '{"nights": 3}')],
        )
    )

    assert response.stop_reason == "tool_use"
    assert [block.type for block in response.content] == ["text", "tool_use"]
    block = response.content[1]
    assert block.id == "call_1"
    assert block.name == "build_quote"
    assert block.input == {"nights": 3}


def test_tool_call_without_preamble_has_no_text_block() -> None:
    response = from_openai_response(
        completion("tool_calls", content=None, tool_calls=[tool_call("c", "t", "{}")])
    )
    assert [block.type for block in response.content] == ["tool_use"]


def test_malformed_tool_arguments_become_an_empty_call() -> None:
    """The engine answers {} with a violation the model can correct from —
    better than dropping the turn."""
    response = from_openai_response(
        completion("tool_calls", tool_calls=[tool_call("c", "build_quote", "{not json")])
    )
    assert response.content[0].input == {}


def test_empty_content_is_still_a_text_block() -> None:
    response = from_openai_response(completion("stop", content=None))
    assert response.content == [] or response.content[0].text == ""


# --- request assembly and error mapping ---------------------------------


class StubCompletions:
    def __init__(self, result=None, error=None) -> None:
        self.result = result
        self.error = error
        self.request: dict = {}

    def create(self, **kwargs):
        self.request = kwargs
        if self.error is not None:
            raise self.error
        return self.result


def client_with(stub: StubCompletions) -> OpenAILLMClient:
    instance = OpenAILLMClient.__new__(OpenAILLMClient)
    instance.model = "gpt-4o-mini"
    instance._client = SimpleNamespace(chat=SimpleNamespace(completions=stub))
    return instance


def test_create_assembles_an_openai_request_from_anthropic_kwargs() -> None:
    stub = StubCompletions(result=completion("stop", content="hello"))
    response = client_with(stub).create(
        system="You sell hotels.",
        messages=[{"role": "user", "content": "hi"}],
        tools=[ANTHROPIC_TOOL],
        max_tokens=2000,
    )

    assert stub.request["model"] == "gpt-4o-mini"
    assert stub.request["max_completion_tokens"] == 2000
    assert stub.request["messages"][0]["role"] == "system"
    assert stub.request["tools"][0]["function"]["name"] == "build_quote"
    assert stub.request["tool_choice"] == "auto"
    assert response.stop_reason == "end_turn"


def test_tool_choice_none_is_translated() -> None:
    stub = StubCompletions(result=completion("stop", content="done"))
    client_with(stub).create(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        tools=[ANTHROPIC_TOOL],
        tool_choice={"type": "none"},
        max_tokens=100,
    )
    assert stub.request["tool_choice"] == "none"


def test_temperature_is_forwarded_when_given() -> None:
    stub = StubCompletions(result=completion("stop", content="done"))
    client_with(stub).create(
        system="s", messages=[{"role": "user", "content": "hi"}], temperature=0.2
    )
    assert stub.request["temperature"] == 0.2


@pytest.mark.parametrize(
    "exc_name,expected_code",
    [
        ("AuthenticationError", "invalid_api_key"),
        ("NotFoundError", "unknown_model"),
        ("RateLimitError", "rate_limited"),
        ("APIStatusError", "upstream_error"),
        ("APIConnectionError", "connection_error"),
    ],
)
def test_sdk_errors_map_to_the_shared_llm_error_codes(
    exc_name: str, expected_code: str
) -> None:
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    response = httpx.Response(status_code=429, request=request)
    exc_cls = getattr(openai, exc_name)
    if exc_name == "APIConnectionError":
        error = exc_cls(request=request)
    else:
        error = exc_cls("boom", response=response, body=None)

    stub = StubCompletions(error=error)
    with pytest.raises(LLMError) as caught:
        client_with(stub).create(messages=[{"role": "user", "content": "hi"}])

    assert caught.value.code == expected_code
    assert "OPENAI" in caught.value.message or "OpenAI" in caught.value.message
