import copy

from graphs.delivery.phase_split import split_same_phase_needs


def test_a_single_same_phase_edge_moves_the_needing_task_to_a_new_phase_after_the_original():
    decomposition = {
        "phases": [{"id": "p", "goal": "Build it"}, {"id": "q", "goal": "Ship it"}],
        "tasks": [
            {"id": "a", "phase": "p", "needs": []},
            {"id": "b", "phase": "p", "needs": ["a", "x-foreign"]},
            {"id": "c", "phase": "q", "needs": ["b"]},
        ],
    }
    before = copy.deepcopy(decomposition)
    result, moves = split_same_phase_needs(decomposition)
    assert result == {
        "phases": [
            {"id": "p", "goal": "Build it"},
            {"id": "p-b", "goal": "Build it"},
            {"id": "q", "goal": "Ship it"},
        ],
        "tasks": [
            {"id": "a", "phase": "p", "needs": []},
            {"id": "b", "phase": "p-b", "needs": ["a", "x-foreign"]},
            {"id": "c", "phase": "q", "needs": ["b"]},
        ],
    }
    assert moves == [{"task": "b", "from": "p", "to": "p-b", "needs": ["a"]}]
    assert decomposition == before


def test_two_chained_edges_in_one_phase_yield_phases_p_then_p_b_then_p_c():
    decomposition = {
        "phases": [{"id": "p", "goal": "G"}],
        "tasks": [
            {"id": "a", "phase": "p", "needs": []},
            {"id": "b", "phase": "p", "needs": ["a"]},
            {"id": "c", "phase": "p", "needs": ["b"]},
        ],
    }
    result, moves = split_same_phase_needs(decomposition)
    assert [p["id"] for p in result["phases"]] == ["p", "p-b", "p-c"]
    assert [p["goal"] for p in result["phases"]] == ["G", "G", "G"]
    assert [(t["id"], t["phase"]) for t in result["tasks"]] == [("a", "p"), ("b", "p-b"), ("c", "p-c")]
    assert moves == [
        {"task": "b", "from": "p", "to": "p-b", "needs": ["a"]},
        {"task": "c", "from": "p", "to": "p-c", "needs": ["b"]},
    ]


def test_a_decomposition_with_no_same_phase_edge_is_returned_equal_with_no_moves():
    decomposition = {
        "phases": [{"id": "p", "goal": "G"}, {"id": "q", "goal": "H"}],
        "tasks": [
            {"id": "a", "phase": "p", "needs": []},
            {"id": "b", "phase": "q", "needs": ["a", "elsewhere"]},
        ],
    }
    result, moves = split_same_phase_needs(decomposition)
    assert result == decomposition
    assert moves == []
