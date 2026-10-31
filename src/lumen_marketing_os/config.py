from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from lumen_marketing_os.core.errors import ConfigurationError


@dataclass(frozen=True)
class Settings:
    workspace: Path
    database: Path
    pause_file: Path
    environment: str
    dry_run: bool
    external_actions_enabled: bool
    busy_timeout_ms: int
    default_lease_seconds: int
    default_max_attempts: int
    retry_base_seconds: int
    retry_max_seconds: int

    def __post_init__(self) -> None:
        if self.environment not in {"development", "staging", "live"}:
            raise ConfigurationError("environment must be development, staging, or live")
        if self.environment != "live" and (not self.dry_run or self.external_actions_enabled):
            raise ConfigurationError("non-live environments cannot enable external actions")
        if self.dry_run and self.external_actions_enabled:
            raise ConfigurationError("dry_run and external_actions_enabled cannot both be true")
        for name in ("busy_timeout_ms", "default_lease_seconds", "default_max_attempts",
                     "retry_base_seconds", "retry_max_seconds"):
            if getattr(self, name) <= 0:
                raise ConfigurationError(f"{name} must be positive")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ConfigurationError("retry_max_seconds must be >= retry_base_seconds")
        if self.pause_file.parent != self.workspace:
            raise ConfigurationError("pause file must be directly inside the workspace")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"cannot read configuration {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigurationError(f"configuration {path} must be a JSON object")
    return data


def _local_path(workspace: Path, value: str) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else workspace / path).resolve()


def load_settings(workspace: Path | None = None, environment: str | None = None) -> Settings:
    root = (workspace or Path(os.environ.get("LMOS_WORKSPACE_ROOT", Path.cwd()))).resolve()
    profile = environment or os.environ.get("LMOS_ENVIRONMENT", "development")
    base = _read_json(root / "config" / "base.json")
    config = base | _read_json(root / "config" / f"{profile}.json")
    if config.get("environment") != profile:
        raise ConfigurationError("profile environment does not match selected environment")
    required = {"database", "pause_file", "environment", "dry_run", "external_actions_enabled",
                "busy_timeout_ms", "default_lease_seconds", "default_max_attempts",
                "retry_base_seconds", "retry_max_seconds"}
    missing = required - config.keys()
    if missing:
        raise ConfigurationError(f"missing configuration keys: {', '.join(sorted(missing))}")
    if any(type(config[k]) is not str or not config[k].strip()
           for k in ("database", "pause_file", "environment")):
        raise ConfigurationError("database, pause_file and environment must be non-empty strings")
    if any(type(config[k]) is not bool for k in ("dry_run", "external_actions_enabled")):
        raise ConfigurationError("dry_run and external_actions_enabled must be booleans")
    integer_keys = ("busy_timeout_ms", "default_lease_seconds", "default_max_attempts",
                    "retry_base_seconds", "retry_max_seconds")
    if any(type(config[k]) is not int for k in integer_keys):
        raise ConfigurationError("runtime limits must be integers")
    return Settings(
        workspace=root,
        database=_local_path(root, config["database"]),
        pause_file=_local_path(root, config["pause_file"]),
        environment=profile,
        dry_run=config["dry_run"],
        external_actions_enabled=config["external_actions_enabled"],
        busy_timeout_ms=config["busy_timeout_ms"],
        default_lease_seconds=config["default_lease_seconds"],
        default_max_attempts=config["default_max_attempts"],
        retry_base_seconds=config["retry_base_seconds"],
        retry_max_seconds=config["retry_max_seconds"],
    )
