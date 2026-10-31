from pathlib import Path
import tomllib


def test_four_project_scoped_codex_roles_are_parseable():
    workspace = Path(__file__).parents[1]
    config_path = workspace / ".codex" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    agents = config["agents"]
    expected = {"trend_explorer", "forum_researcher", "competitor_auditor", "directory_sieve"}
    assert agents["enabled"] is True
    assert expected <= set(agents)
    for name in expected:
        role_path = config_path.parent / agents[name]["config_file"]
        role = tomllib.loads(role_path.read_text(encoding="utf-8"))
        assert len(role["developer_instructions"]) > 100
        assert ".env" in role["developer_instructions"]


def test_department_six_roles_have_matching_guides():
    workspace = Path(__file__).parents[1]
    config_path = workspace / ".codex" / "config.toml"
    agents = tomllib.loads(config_path.read_text(encoding="utf-8"))["agents"]
    expected = {
        "reddit_inbound_hacker": "6A_REDDIT.md",
        "hacker_news_content_strategist": "6B_HACKER_NEWS.md",
        "product_hunt_launchpads_orchestrator": "6C_LAUNCHPADS.md",
        "linkedin_authority_builder": "6D_LINKEDIN.md",
        "b2b_prospect_list_indexer": "6E_PROSPECT_INDEXER.md",
    }
    for name, guide_name in expected.items():
        role = tomllib.loads((config_path.parent / agents[name]["config_file"]).read_text(encoding="utf-8"))
        guide = workspace / "DEPARTMENT_6_AGENT_GUIDES" / guide_name
        assert guide.is_file() and len(guide.read_text(encoding="utf-8")) > 500
        assert guide_name in role["developer_instructions"]
