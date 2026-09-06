"""resolve_cartridge forwards the overlay it is given straight to core's load."""

from __future__ import annotations

from harness.cli import _read_overlay
from harness.resolve import overlay_path, resolve_cartridge


def test_overlay_path_joins_the_repo_to_the_fixed_location() -> None:
    assert overlay_path("/repo") == "/repo/.agent/cartridge.yaml"


def test_an_overlay_mapping_is_forwarded_to_load(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_load(team, cartridges_dir, *, skill_index, overlay=None):
        seen["overlay"] = overlay
        return {"team": team}

    monkeypatch.setattr("harness.resolve.load", fake_load)
    overlay = {"description": "project layer"}

    resolve_cartridge(
        "acme", cartridges_dir=tmp_path, skills_root=[tmp_path], unverified_skills=True, overlay=overlay
    )

    assert seen["overlay"] == overlay


def test_no_overlay_forwards_none(monkeypatch, tmp_path) -> None:
    seen = {}

    def fake_load(team, cartridges_dir, *, skill_index, overlay=None):
        seen["overlay"] = overlay
        return {"team": team}

    monkeypatch.setattr("harness.resolve.load", fake_load)

    resolve_cartridge("acme", cartridges_dir=tmp_path, skills_root=[tmp_path], unverified_skills=True)

    assert seen["overlay"] is None


def test_read_overlay_parses_the_file_when_the_repo_has_one(tmp_path) -> None:
    agent_dir = tmp_path / ".agent"
    agent_dir.mkdir()
    (agent_dir / "cartridge.yaml").write_text("description: project layer\n", encoding="utf-8")

    assert _read_overlay(str(tmp_path)) == {"description": "project layer"}


def test_read_overlay_is_none_when_the_repo_has_no_overlay_file(tmp_path) -> None:
    assert _read_overlay(str(tmp_path)) is None
