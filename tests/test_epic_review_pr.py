"""The escalated draft opens a review PR; the task stays `approved` and nothing merges."""

from __future__ import annotations

from harness import epic as epic_module
from tests import test_epic_driver as base

repo = base.repo
cart = base.cart

URL = "https://forge.example/pr/1"


def _run(repo, cart, tmp_path, monkeypatch, *, push):
    pushes: list[tuple[str, str]] = []
    prs: list[dict[str, str]] = []

    def fake_push(ctx, task):
        pushes.append((task, f"review/{ctx.initiative_id}/{task}"))
        return push

    def fake_pr(ctx, *, title, body, head, base):
        prs.append({"title": title, "body": body, "head": head, "base": base})
        return True, URL

    monkeypatch.setattr(epic_module, "_push_review_branch", fake_push)
    monkeypatch.setattr(epic_module, "_open_review_pr", fake_pr)
    runner = base.Runner(
        {"t1-probe": base.new_file_patch("harness/x.py"), "t2-bench": base.new_file_patch("t2-bench.txt")}
    )
    result, _ = base.drive(repo, cart, tmp_path, runner=runner, work=base.initiative(two_phases=False))
    record = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    return pushes, prs, record


def test_an_escalated_draft_pushes_and_opens_one_review_pr(repo, cart, tmp_path, monkeypatch) -> None:
    pushes, prs, record = _run(repo, cart, tmp_path, monkeypatch, push=(True, "pushed"))

    assert pushes == [("t1-probe", "review/demo-initiative/t1-probe")]
    assert [p["title"] for p in prs] == ["[review] demo-initiative: schema probe"]
    assert prs[0]["head"] == "review/demo-initiative/t1-probe"
    assert "Governance paths: harness/x.py" in prs[0]["body"]
    assert record["status"] == "approved"
    assert record["review_pr"] == URL
    assert not base.is_ancestor(repo, "agents/epic-1/t1-probe", "epic/demo-initiative/p1-foundations")


def test_a_failed_push_opens_no_pr_and_records_none(repo, cart, tmp_path, monkeypatch) -> None:
    pushes, prs, record = _run(repo, cart, tmp_path, monkeypatch, push=(False, "no origin"))

    assert pushes == [("t1-probe", "review/demo-initiative/t1-probe")]
    assert prs == []
    assert "review_pr" not in record
