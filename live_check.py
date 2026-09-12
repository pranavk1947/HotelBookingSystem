"""Run one real conversation against each hotel and report what happened.

    python live_check.py            # uses whichever provider .env resolves to
    LLM_PROVIDER=openai python live_check.py

Prints the provider and model, whether build_quote fired, and the first reply.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "server"))

from src.agent.agent_service import AgentService  # noqa: E402
from src.agent.llm_client import AnthropicLLMClient  # noqa: E402
from src.agent.openai_client import OpenAILLMClient  # noqa: E402
from src.agent.prompt_builder import SystemPromptBuilder  # noqa: E402
from src.config.settings import get_settings  # noqa: E402
from src.model.chat import ChatMessage  # noqa: E402
from src.repository.config_repository import FileHotelConfigRepository  # noqa: E402


def main() -> int:
    settings = get_settings()
    if not settings.has_api_key:
        print("No API key set. Add ANTHROPIC_API_KEY or OPENAI_API_KEY to .env.")
        return 1

    if settings.provider == "openai":
        llm = OpenAILLMClient(settings.openai_api_key, settings.openai_model)
    else:
        llm = AnthropicLLMClient(settings.anthropic_api_key, settings.anthropic_model)

    print(f"provider : {settings.provider}")
    print(f"model    : {settings.active_model}")

    repo = FileHotelConfigRepository(settings.configs_dir)
    agent = AgentService(llm, SystemPromptBuilder(), settings)

    for summary in repo.list_summaries():
        config = repo.get(summary.id)
        opener = summary.sample_opener
        print("\n" + "=" * 78)
        print(f"{config.name}  ({summary.id})")
        print("=" * 78)
        print(f"USER: {opener}\n")

        result = agent.run_turn(
            config, [ChatMessage(role="user", content=opener)], date.today()
        )
        print(f"AGENT:\n{result.reply}\n")
        print(f"build_quote fired : {result.quote is not None}")
        if result.quote is not None:
            q = result.quote
            print(f"lines             : {len(q.lines)}")
            print(f"subtotal / total  : {q.subtotal:,.2f} / {q.total:,.2f}")
            if q.warnings:
                print(f"warnings          : {[w.code for w in q.warnings]}")
        print(f"rounds / tokens   : {result.usage.rounds} / "
              f"{result.usage.input_tokens}in {result.usage.output_tokens}out")
        if result.error:
            print(f"ERROR             : {result.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
