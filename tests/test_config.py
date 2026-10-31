from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumen_marketing_os.config import load_settings
from lumen_marketing_os.core.errors import ConfigurationError


def test_safe_default_and_conflicting_live_flags(tmp_path):
    source = Path(__file__).resolve().parents[1] / "config"
    config = tmp_path / "config"
    config.mkdir()
    for name in ("base.json", "development.json", "live.json"):
        (config / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    settings = load_settings(tmp_path)
    assert settings.dry_run is True
    assert settings.external_actions_enabled is False
    assert settings.pause_file == tmp_path / "PAUSE_ALL.lock"
    live = json.loads((config / "live.json").read_text(encoding="utf-8"))
    live["external_actions_enabled"] = True
    (config / "live.json").write_text(json.dumps(live), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_settings(tmp_path, "live")
