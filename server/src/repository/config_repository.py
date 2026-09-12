"""Config persistence: one JSON file per hotel, behind a Protocol.

Nothing is cached — re-reading on every request is what makes a config edit take
effect on the next chat message with no restart.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from src.model.hotel_config import CONFIG_ID_PATTERN
from src.model.hotel_config import ConfigSummary
from src.model.hotel_config import HotelConfig

_ID_RE = re.compile(CONFIG_ID_PATTERN)


class ConfigNotFoundError(Exception):
    def __init__(self, config_id: str) -> None:
        super().__init__(f"No config named '{config_id}'")
        self.config_id = config_id


class DuplicateConfigError(Exception):
    def __init__(self, config_id: str) -> None:
        super().__init__(f"A config named '{config_id}' already exists")
        self.config_id = config_id


class InvalidConfigIdError(Exception):
    def __init__(self, config_id: str) -> None:
        super().__init__(
            f"'{config_id}' is not a valid config id "
            "(lowercase letters, digits and hyphens, 2-49 characters)"
        )
        self.config_id = config_id


class LastConfigError(Exception):
    def __init__(self) -> None:
        super().__init__("Cannot delete the last remaining config")


class ConfigValidationError(Exception):
    """Carries field-level errors so the config page can point at the bad field."""

    def __init__(self, errors: list[dict]) -> None:
        super().__init__("Config failed validation")
        self.errors = errors


class HotelConfigRepository(Protocol):
    def list_summaries(self) -> list[ConfigSummary]: ...
    def get(self, config_id: str) -> HotelConfig: ...
    def create(self, config: HotelConfig) -> HotelConfig: ...
    def update(self, config_id: str, config: HotelConfig) -> HotelConfig: ...
    def delete(self, config_id: str) -> None: ...


class FileHotelConfigRepository:
    """Reads and writes ``<configs_dir>/<id>.json``."""

    def __init__(self, configs_dir: Path) -> None:
        self.dir = Path(configs_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    # --- reads ----------------------------------------------------------

    def list_summaries(self) -> list[ConfigSummary]:
        summaries: list[ConfigSummary] = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                config = self._read(path)
            except (ConfigValidationError, json.JSONDecodeError):
                # One broken file must not empty the whole dropdown.
                continue
            summaries.append(
                ConfigSummary(
                    id=config.id,
                    name=config.name,
                    currency=config.currency,
                    sample_opener=config.sample_opener,
                )
            )
        return summaries

    def get(self, config_id: str) -> HotelConfig:
        return self._read(self._path(config_id))

    def raw(self, config_id: str) -> dict:
        """The file as-is, so the editor can open a config that fails validation."""
        path = self._path(config_id)
        if not path.exists():
            raise ConfigNotFoundError(config_id)
        return json.loads(path.read_text(encoding="utf-8"))

    # --- writes ---------------------------------------------------------

    def create(self, config: HotelConfig) -> HotelConfig:
        path = self._path(config.id)
        if path.exists():
            raise DuplicateConfigError(config.id)
        self._write(path, config)
        return config

    def update(self, config_id: str, config: HotelConfig) -> HotelConfig:
        path = self._path(config_id)
        if not path.exists():
            raise ConfigNotFoundError(config_id)
        if config.id != config_id:
            # Renaming an id would orphan the old file and break any open chat.
            raise ConfigValidationError(
                [{"field": "id", "message": f"id must stay '{config_id}' when saving"}]
            )
        self._write(path, config)
        return config

    def delete(self, config_id: str) -> None:
        path = self._path(config_id)
        if not path.exists():
            raise ConfigNotFoundError(config_id)
        if len(list(self.dir.glob("*.json"))) <= 1:
            raise LastConfigError
        path.unlink()

    # --- internals ------------------------------------------------------

    def _path(self, config_id: str) -> Path:
        # The id becomes a filename: validate the slug, then confirm the resolved
        # path is still inside the configs dir.
        if not _ID_RE.match(config_id or ""):
            raise InvalidConfigIdError(config_id)
        path = (self.dir / f"{config_id}.json").resolve()
        if path.parent != self.dir.resolve():
            raise InvalidConfigIdError(config_id)
        return path

    def _read(self, path: Path) -> HotelConfig:
        if not path.exists():
            raise ConfigNotFoundError(path.stem)
        return parse_config(json.loads(path.read_text(encoding="utf-8")))

    def _write(self, path: Path, config: HotelConfig) -> None:
        payload = json.dumps(
            config.model_dump(mode="json"), indent=2, ensure_ascii=False
        )
        # Atomic: a crash mid-save must not leave a truncated config behind.
        with tempfile.NamedTemporaryFile(
            "w", dir=self.dir, delete=False, encoding="utf-8", suffix=".tmp"
        ) as handle:
            handle.write(payload + "\n")
            tmp_name = handle.name
        os.replace(tmp_name, path)


def parse_config(payload: dict) -> HotelConfig:
    """Validate a config dict, raising field-level errors the UI can render."""
    try:
        return HotelConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfigValidationError(format_errors(exc)) from exc


def format_errors(exc: ValidationError) -> list[dict]:
    """Pydantic errors -> ``[{field, message}]`` keyed by dotted path."""
    formatted: list[dict] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"] if part != "__root__")
        formatted.append({"field": location or "(root)", "message": error["msg"]})
    return formatted
