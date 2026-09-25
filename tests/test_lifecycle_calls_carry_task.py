"""Every node call that serves one ticket names that ticket; a run-level call names none.

The runners write `task` into the store's node_calls row as task_id, so a call
that omits it is a row nobody can attribute to a ticket.
"""

from __future__ import annotations

import pytest

from graphs.delivery import lifecycle_propose
from runner import ScriptedRunner

TICKET = "T-1"
PER_TICKET_ROLES = {
    "plan", "plan_alternative", "plan_arbitrate", "plan_adversary",
    "build", "handoff", "review_charter", "review_adversary", "arbitrate",
}

APPROVE = {"verdict": "approve", "findings": [], "rationale": "matches the charter"}
PLACEHOLDER = {"verdict": "revise", "findings": [], "rationale": ""}
ADV_REJECT = {
    "verdict": "reject",
    "objections": [{"claim": "it is safe", "why_wrong": "it drops a column"}],
    "strongest_objection": "it drops a column",
}
ARB = {"verdict": "approve", "sided_with": "charter", "reasoning": "the objection is about style"}
PLAN_B = {"steps": ["add a guard"], "files_expected": ["src/b.py"], "out_of_scope": ["src/a.py"]}
CHOOSE_FIRST = {"chosen": "first", "plan": PLAN_B, "reasoning": "a is checkable", "price": "b unwritten"}
ATTACK_REVISE = {
    "verdict": "revise",
    "objections": [{"claim": "names a Reader class", "why_wrong": "there is no Reader"}],
    "strongest_objection": "names a Reader class that does not exist",
}
HANDOFF = {"complete": True, "blocking": False, "missing": [], "brief": "one file, tests green"}
SCOPE = {"phases": [], "tickets": [], "repos": [], "state": "planned", "rationale": "one ticket"}


class Spy(ScriptedRunner):
    """ScriptedRunner does not record `task`; this keeps the kwargs of every call."""

    def __init__(self, responses) -> None:
        super().__init__(responses)
        self.kwargs: list[dict] = []

    def run(self, **kwargs):
        self.kwargs.append(kwargs)
        return super().run(**kwargs)


def bound(cartridge, *roles) -> dict:
    cartridge["work_routing"] = {"states": {"planned": "the planned column"}}
    cartridge["write_kinds"]["item_create"] = {"risk": "low", "ramp": "gated"}
    cartridge["policy"] = {"review_tier": {"tier2_surfaces": ["migration"]}}
    cartridge["skills"].update({role: f"acme-skills:{role}" for role in roles})
    return cartridge


def drive(cartridge, responses, **args):
    spy = Spy(responses)
    lifecycle_propose.run(
        {"run_id": "r", "date": "2026-09-25", "ticket": TICKET, "cartridge": cartridge, **args}, spy
    )
    return spy


def test_every_per_ticket_call_carries_the_ticket_id_and_scope_epic_carries_none(
    cartridge, plan_response, build_response
) -> None:
    cartridge = bound(
        cartridge, "scope_epic", "plan_alternative", "plan_arbitrate", "plan_adversary",
        "handoff", "review_adversary", "arbitrate",
    )
    spy = drive(
        cartridge,
        {
            "scope_epic": SCOPE,
            "plan": [plan_response, plan_response],
            "plan_alternative": PLAN_B,
            "plan_arbitrate": CHOOSE_FIRST,
            "plan_adversary": ATTACK_REVISE,
            "build": build_response,
            "handoff": HANDOFF,
            "review_charter": APPROVE,
            "review_adversary": ADV_REJECT,
            "arbitrate": ARB,
        },
        surfaces=["migration"],
    )
    seen = {kw["role"] for kw in spy.kwargs}
    assert seen >= PER_TICKET_ROLES, f"a role stopped running: {PER_TICKET_ROLES - seen}"
    assert [kw["role"] for kw in spy.kwargs].count("plan") == 2, "first plan and the revision"
    for kw in spy.kwargs:
        if kw["role"] in PER_TICKET_ROLES:
            assert kw.get("task") == TICKET, kw["role"]
    scope_calls = [kw for kw in spy.kwargs if kw["role"] == "scope_epic"]
    assert scope_calls and all(kw.get("task") is None for kw in scope_calls)


def test_a_reviewer_retry_after_a_placeholder_carries_the_ticket_id_too(
    cartridge, plan_response, build_response
) -> None:
    spy = drive(
        bound(cartridge),
        {"plan": plan_response, "build": build_response, "review_charter": [PLACEHOLDER, APPROVE]},
    )
    reviews = [kw for kw in spy.kwargs if kw["role"] == "review_charter"]
    assert len(reviews) == 2
    assert [kw.get("task") for kw in reviews] == [TICKET, TICKET]


@pytest.mark.parametrize("role", ["review_charter", "review_adversary"])
def test_review_entry_reviews_a_bare_diff_and_names_no_ticket(role) -> None:
    """The default keeps review_entry, which has no ticket, exactly as it was."""
    spy = Spy({role: APPROVE if role == "review_charter" else {"verdict": "approve", "objections": [], "strongest_objection": "none"}})
    lifecycle_propose._reviewer_answer(spy, role=role, schema={}, context=[], prompt="p")
    assert spy.kwargs[0].get("task") is None
