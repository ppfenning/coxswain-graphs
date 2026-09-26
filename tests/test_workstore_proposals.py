from types import SimpleNamespace

from harness.epic import _build_batch, item_apply, state_move_apply

CARTRIDGE = {"write_kinds": {"state_move": {"risk": "low", "ramp": "deferred"}, "merge_stack": {"risk": "low", "ramp": "deferred"}, "consolidate": {"risk": "low", "ramp": "deferred"}}}
ITEM = {"id": "t1", "title": "Probe", "state": "ready", "needs": [], "surfaces": ["a.py"], "body": "text"}


def _state_move(by_id):
    ctx = SimpleNamespace(
        cartridge=CARTRIDGE,
        draft_branch=lambda phase, task: f"draft/{task}",
        phase_branch=lambda phase: f"phase/{phase}",
        ledger_path=None,
    )
    built = {"t1": {"proposals": [], "result": {"review": {"verdict": "approve"}}, "evidence": [{"check": "pytest", "output": "1 passed"}]}}
    batch, _ = _build_batch(
        ctx, phase="p1", surviving=["t1"], built=built, escalated=set(), chunk_by_task={}, rebase=None, by_id=by_id
    )
    return next(p for p in batch if p["kind"] == "state_move")


def test_state_move_carries_the_path_and_new_state_and_keeps_the_human_text() -> None:
    move = _state_move({"t1": {"path": "work/i/p1/t1.md"}})
    assert move["apply"] == {"path": "work/i/p1/t1.md", "state": "approved"}
    assert move["suggested_action"] == "mark t1 approved"
    assert move["rationale"] == "t1 was built, checked and reviewed in this run"


def test_state_move_apply_is_path_and_state() -> None:
    assert state_move_apply("work/x.md", "approved") == {"apply": {"path": "work/x.md", "state": "approved"}}


def test_item_create_apply_is_path_and_full_item() -> None:
    assert item_apply("work/i/p1/t1.md", ITEM) == {"apply": {"path": "work/i/p1/t1.md", "item": ITEM}}


def test_item_update_apply_is_path_and_full_item() -> None:
    changed = {**ITEM, "state": "done"}
    assert item_apply("work/i/p1/t1.md", changed)["apply"]["item"]["state"] == "done"


def test_no_known_path_emits_no_apply_key() -> None:
    assert "apply" not in _state_move({"t1": {}})
    assert "apply" not in _state_move({})
    assert state_move_apply(None, "approved") == {}
    assert item_apply(None, ITEM) == {}
