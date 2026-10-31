from __future__ import annotations

import json
from pathlib import Path
import shutil

from lumen_marketing_os.cli.main import _print, main


def test_cli_json_is_windows_console_safe(capsys):
    _print({"title": "SaaS \u2014 launch \U0001f680"})
    output = capsys.readouterr().out
    output.encode("cp1252")
    assert json.loads(output)["title"] == "SaaS \u2014 launch \U0001f680"


def test_local_cli_init_status_pause_and_queue(tmp_path, capsys):
    source = Path(__file__).resolve().parents[1] / "config"
    config = tmp_path / "config"
    config.mkdir()
    for name in ("base.json", "development.json"):
        (config / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    prefix = ["--workspace", str(tmp_path)]
    assert main(prefix + ["status"]) == 0
    assert json.loads(capsys.readouterr().out)["initialized"] is False
    assert main(prefix + ["init"]) == 0
    capsys.readouterr()
    assert main(prefix + ["enqueue", "research.scan", "--lane", "research",
                          "--key", "first", "--payload", '{"q":"x"}']) == 0
    job_id = json.loads(capsys.readouterr().out)["id"]
    assert main(prefix + ["pause", "--actor", "owner", "--reason", "incident"]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is True
    assert main(prefix + ["claim", "--worker", "worker"]) == 2
    assert "paused" in capsys.readouterr().err
    assert main(prefix + ["resume", "--actor", "owner", "--reason", "fixed"]) == 0
    capsys.readouterr()
    assert main(prefix + ["claim", "--worker", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == job_id
    assert main(prefix + ["complete", job_id, "--worker", "worker"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "succeeded"
    assert main(prefix + ["check"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert main(prefix + ["pause", "--lane", "email", "--actor", "owner",
                          "--reason", "mail issue"]) == 0
    capsys.readouterr()
    assert main(prefix + ["status", "--lane", "email"]) == 0
    lane_status = json.loads(capsys.readouterr().out)
    assert lane_status["paused"] is True
    assert lane_status["active_pause_scopes"][0]["scope"] == "lane:email"


def test_community_cli_creates_durable_task_without_browser_connection(tmp_path, capsys):
    workspace = Path(__file__).resolve().parents[1]
    config = tmp_path / "config"
    config.mkdir()
    for name in ("base.json", "development.json"):
        (config / name).write_text((workspace / "config" / name).read_text(encoding="utf-8"),
                                   encoding="utf-8")
    shutil.copytree(workspace / "DEPARTMENT_6_AGENT_GUIDES", tmp_path / "DEPARTMENT_6_AGENT_GUIDES")
    payload = tmp_path / "thread.json"
    payload.write_text('{"thread_url":"https://www.reddit.com/r/test/comments/42"}', encoding="utf-8")
    prefix = ["--workspace", str(tmp_path)]
    assert main(prefix + ["init"]) == 0
    capsys.readouterr()
    assert main(prefix + ["community", "trigger", "6A", "--key", "thread:42",
                          "--input-file", str(payload)]) == 0
    task_id = json.loads(capsys.readouterr().out)["task_id"]
    assert main(prefix + ["community", "brief", task_id]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["agent_id"] == "6A" and brief["state"] == "pending"
    assert main(prefix + ["community", "claim", "6A", "--worker", "codex-6A"]) == 2
    assert "connection" in capsys.readouterr().err
