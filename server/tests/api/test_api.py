"""End-to-end API tests over HTTP, following the three flows a reviewer runs.

The LLM is stubbed (no key needed in CI) but everything else is real: the
routers, the file repository, the prompt builder and the pricing engine. What
these pin down is the claim the README makes — that editing a config in the
browser changes the agent's next quote with no restart.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from src.agent.agent_service import AgentService
from src.agent.prompt_builder import SystemPromptBuilder
from src.config.settings import Settings
from src.repository.config_repository import FileHotelConfigRepository

CONFIG_DIR = Path(__file__).resolve().parents[2] / "configs"

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


class ScriptedLLM:
    """Always quotes the same selection, then reports what the engine returned.

    Holding the selection fixed is the point: any change in the quote between
    two calls can only have come from the config.
    """

    def __init__(self, selection: dict) -> None:
        self.selection = selection
        self.systems: list[str] = []
        self._round = 0

    def create(self, **kwargs):
        self.systems.append(kwargs["system"])
        self._round += 1
        usage = SimpleNamespace(input_tokens=10, output_tokens=5)
        if self._round % 2 == 1:
            return SimpleNamespace(
                stop_reason="tool_use",
                usage=usage,
                content=[
                    SimpleNamespace(
                        type="tool_use",
                        id=f"tu_{self._round}",
                        name="build_quote",
                        input=copy.deepcopy(self.selection),
                    )
                ],
            )
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=usage,
            content=[SimpleNamespace(type="text", text="Here is the proposal.")],
        )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """A real app over a throwaway copy of the shipped configs."""
    configs = tmp_path / "configs"
    configs.mkdir()
    for source in CONFIG_DIR.glob("*.json"):
        shutil.copy(source, configs / source.name)

    from src.app import app

    settings = Settings(anthropic_api_key="test-key", configs_dir=configs)
    app.state.settings = settings
    app.state.config_repo = FileHotelConfigRepository(configs)
    app.state.agent_service = AgentService(
        ScriptedLLM(BRIEF_SELECTION), SystemPromptBuilder(), settings
    )
    with TestClient(app) as test_client:
        # TestClient's lifespan re-wires state from the real env; put the test
        # wiring back so the stub is the one that answers.
        app.state.settings = settings
        app.state.config_repo = FileHotelConfigRepository(configs)
        app.state.agent_service = AgentService(
            ScriptedLLM(BRIEF_SELECTION), SystemPromptBuilder(), settings
        )
        yield test_client


def quote_for(client: TestClient, config_id: str) -> dict:
    response = client.post(
        "/api/chat",
        json={"config_id": config_id, "messages": [{"role": "user", "content": "quote"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["error"] is None, body
    return body["quote"]


# --- flow 1: read both configs, switch between them ---------------------


def test_both_sample_configs_are_listed_and_load(client: TestClient) -> None:
    summaries = client.get("/api/configs").json()
    assert {s["id"] for s in summaries} == {"grand-cascadia", "hacienda-del-sol"}
    for summary in summaries:
        assert summary["sample_opener"]
        assert client.get(f"/api/configs/{summary['id']}").status_code == 200


def test_each_agent_quotes_from_its_own_config(client: TestClient) -> None:
    quote = quote_for(client, "grand-cascadia")
    assert quote["hotel_name"] == "The Grand Cascadia"
    assert quote["lines"][0]["unit_price"] == 189.0
    assert quote["adjustments"][0]["label"] == "Volume discount (10% on rooms ≥ 30)"

    # The same selection sent to the other hotel names items it does not stock,
    # so nothing is priced — a config can only ever sell its own catalog.
    response = client.post(
        "/api/chat",
        json={
            "config_id": "hacienda-del-sol",
            "messages": [{"role": "user", "content": "quote"}],
        },
    )
    assert response.json()["quote"] is None


def test_each_agent_is_briefed_on_its_own_catalog_and_rules(client: TestClient) -> None:
    quote_for(client, "grand-cascadia")
    cascadia = client.app.state.agent_service.llm.systems[-1]
    assert "grand-ballroom" in cascadia
    assert "Volume discount (10% on rooms ≥ 30)" in cascadia
    assert "casita-king" not in cascadia

    client.post(
        "/api/chat",
        json={
            "config_id": "hacienda-del-sol",
            "messages": [{"role": "user", "content": "quote"}],
        },
    )
    hacienda = client.app.state.agent_service.llm.systems[-1]
    assert "casita-king" in hacienda
    assert "Length-of-stay discount (12% on 3+ nights)" in hacienda
    assert "Spring peak" in hacienda          # seasonal rates reach the prompt
    assert "2 nights" in hacienda             # and so does the minimum stay
    assert "grand-ballroom" not in hacienda


# --- flow 2: edit a config, re-run, see the agent adapt -----------------


def test_editing_a_price_changes_the_next_quote_with_no_restart(
    client: TestClient,
) -> None:
    before = quote_for(client, "grand-cascadia")
    assert before["lines"][0]["subtotal"] == 22_680.00

    config = client.get("/api/configs/grand-cascadia").json()
    for item in config["inventory"]:
        if item["id"] == "std-room":
            item["unit_price"] = 250.0
    assert client.put("/api/configs/grand-cascadia", json=config).status_code == 200

    after = quote_for(client, "grand-cascadia")
    assert after["lines"][0]["unit_price"] == 250.0
    assert after["lines"][0]["subtotal"] == 30_000.00       # 40 × 250 × 3
    assert after["adjustments"][0]["amount"] == -3_000.00   # 10% of the new figure


def test_editing_a_rule_changes_the_discount(client: TestClient) -> None:
    config = client.get("/api/configs/grand-cascadia").json()
    config["rules"][0]["discount_pct"] = 25.0
    config["rules"][0]["label"] = "Volume discount (25% on rooms ≥ 30)"
    config["rules"][0]["min_qty"] = 41  # now above the 40 rooms being booked
    assert client.put("/api/configs/grand-cascadia", json=config).status_code == 200

    quote = quote_for(client, "grand-cascadia")
    assert quote["adjustments"] == []  # threshold raised past the booking

    config["rules"][0]["min_qty"] = 30
    client.put("/api/configs/grand-cascadia", json=config)
    quote = quote_for(client, "grand-cascadia")
    assert quote["adjustments"][0]["amount"] == -5_670.00   # 25% of 22,680


def test_edited_config_reaches_the_system_prompt_too(client: TestClient) -> None:
    """A config edit must change what the agent *says*, not just what it bills."""
    config = client.get("/api/configs/grand-cascadia").json()
    config["persona"] = "You are Wilhelmina, and you always mention the rooftop."
    client.put("/api/configs/grand-cascadia", json=config)

    quote_for(client, "grand-cascadia")
    system = client.app.state.agent_service.llm.systems[-1]
    assert "Wilhelmina" in system
    assert "rooftop" in system


# --- flow 3: add a brand-new config, chat against it --------------------


def test_a_new_config_can_be_added_and_immediately_quoted(client: TestClient) -> None:
    source = client.get("/api/configs/grand-cascadia").json()
    new = copy.deepcopy(source)
    new["id"] = "riverside-inn"
    new["name"] = "Riverside Inn"
    new["policies"]["tax_pct"] = 0.0
    new["fees"] = []
    for item in new["inventory"]:
        if item["id"] == "std-room":
            item["unit_price"] = 120.0
    new["rules"] = []

    assert client.post("/api/configs", json=new).status_code == 201
    assert "riverside-inn" in {s["id"] for s in client.get("/api/configs").json()}

    quote = quote_for(client, "riverside-inn")
    assert quote["hotel_name"] == "Riverside Inn"
    assert quote["lines"][0]["subtotal"] == 14_400.00       # 40 × 120 × 3
    assert quote["adjustments"] == []
    assert quote["tax"] is None
    assert quote["total"] == 14_400.00 + 7_200.00 + 3_000.00


def test_a_new_config_is_written_to_its_own_file(client: TestClient, tmp_path: Path) -> None:
    new = client.get("/api/configs/hacienda-del-sol").json()
    new["id"] = "casa-nueva"
    new["name"] = "Casa Nueva"
    client.post("/api/configs", json=new)

    written = tmp_path / "configs" / "casa-nueva.json"
    assert written.exists()
    assert json.loads(written.read_text())["name"] == "Casa Nueva"


# --- guard rails --------------------------------------------------------


def test_invalid_config_is_rejected_with_field_level_errors(client: TestClient) -> None:
    config = client.get("/api/configs/grand-cascadia").json()
    config["inventory"][0]["unit_price"] = -10
    config["policies"]["tax_pct"] = 500

    response = client.put("/api/configs/grand-cascadia", json=config)
    assert response.status_code == 400
    fields = {error["field"] for error in response.json()["errors"]}
    assert "inventory.0.unit_price" in fields
    assert "policies.tax_pct" in fields

    # And the file on disk is untouched.
    assert client.get("/api/configs/grand-cascadia").json()["inventory"][0][
        "unit_price"
    ] == 189.0


def test_validate_never_writes_anything(client: TestClient) -> None:
    config = client.get("/api/configs/grand-cascadia").json()
    config["name"] = "Should Not Persist"
    assert client.post("/api/configs/validate", json=config).json()["valid"] is True
    assert client.get("/api/configs/grand-cascadia").json()["name"] == "The Grand Cascadia"


def test_duplicate_and_missing_ids_are_reported_cleanly(client: TestClient) -> None:
    existing = client.get("/api/configs/grand-cascadia").json()
    assert client.post("/api/configs", json=existing).status_code == 409
    assert client.get("/api/configs/nope").status_code == 404
    assert client.delete("/api/configs/nope").status_code == 404


def test_the_last_config_cannot_be_deleted(client: TestClient) -> None:
    assert client.delete("/api/configs/hacienda-del-sol").status_code == 204
    response = client.delete("/api/configs/grand-cascadia")
    assert response.status_code == 409
    assert response.json()["error_code"] == "LAST_CONFIG"


def test_config_id_cannot_escape_the_configs_directory(client: TestClient) -> None:
    for attempt in ["../secrets", "..%2f..%2fetc%2fpasswd", "Grand-Cascadia"]:
        assert client.get(f"/api/configs/{attempt}").status_code in (400, 404)


def test_health_reports_what_actually_blocks_the_app(client: TestClient) -> None:
    health = client.get("/health").json()
    assert health["status"] == "healthy"
    assert health["api_key_configured"] is True
    assert set(health["configs"]) == {"grand-cascadia", "hacienda-del-sol"}


def test_both_browser_pages_are_served(client: TestClient) -> None:
    for path in ["/", "/config", "/static/chat.js", "/static/config.js", "/static/styles.css"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers.get("cache-control", "")
