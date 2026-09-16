from graphs.delivery.ticket_lint import Problem, lint_tickets


def _task(**over):
    return {"id": "t1", "phase": "p1", "title": "x", "body": "", "needs": [], "surfaces": [], **over}


def test_a_path_outside_the_target_repo_is_a_reach_refusal():
    tasks = [_task(body="notes live at ~/scratch/notes.md")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    problems = lint_tickets(tasks, tree, [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "reach"
    assert problems[0].severity == "refusal"


def test_a_command_the_sandbox_does_not_grant_is_a_grant_advisory():
    tasks = [_task(body="verify with `cox route lint t1`")]
    problems = lint_tickets(tasks, [], ["pytest", "git status", "git diff"], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "grant"
    assert problems[0].severity == "advisory"


def test_a_command_the_sandbox_grants_is_not_a_grant_advisory():
    tasks = [_task(body="verify with `pytest -q tests/test_x.py` then `git diff --stat`")]
    assert lint_tickets(tasks, [], ["pytest", "git status", "git diff"], "graphs") == []


def test_a_fenced_block_with_two_risky_commands_yields_two_grant_advisories():
    tasks = [_task(body="```bash\ncox route lint t1\ngh pr view 5\n```")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 2
    assert {p.rule for p in problems} == {"grant"}
    assert all(p.severity == "advisory" for p in problems)


def test_a_750_word_body_is_a_size_advisory():
    tasks = [_task(body=" ".join(["word"] * 750))]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "size"
    assert problems[0].severity == "advisory"


def test_two_tasks_sharing_a_test_file_with_no_needs_path_is_a_coupling_refusal():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", surfaces=["tests/test_shared.py"]),
    ]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"
    assert problems[0].severity == "refusal"


def test_a_clean_dag_yields_no_problems():
    tasks = [_task(body="does nothing risky", surfaces=["graphs/delivery/ticket_lint.py"])]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_an_in_repo_relative_path_containing_a_workspace_segment_is_not_a_reach_problem():
    tasks = [_task(body="see docs/workspace/notes.md for context", surfaces=["docs/workspace/notes.md"])]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_ticket_naming_workspace_runs_is_refused_with_the_corpus_correction():
    tasks = [_task(body="check workspace/runs/2026-09-08.json for the failure")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "reach"
    assert problems[0].severity == "refusal"
    assert problems[0].fix == "route this to `cox stats` or the chair; the build seat sees one repository worktree"


def test_the_same_ticket_with_the_corpus_path_removed_passes():
    tasks = [_task(body="check the failure in the ticket's own history")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_two_tasks_whose_named_modules_import_one_another_is_a_coupling_refusal():
    tasks = [
        _task(id="t1", surfaces=["graphs/delivery/a.py"]),
        _task(id="t2", surfaces=["graphs/delivery/b.py"]),
    ]
    tree = [
        {"path": "graphs/delivery/a.py", "repo": "graphs", "imports": ["graphs/delivery/b.py"]},
        {"path": "graphs/delivery/b.py", "repo": "graphs"},
    ]
    problems = lint_tickets(tasks, tree, [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"
    assert problems[0].severity == "refusal"


def test_the_same_import_pair_ordered_by_needs_is_not_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["graphs/delivery/a.py"]),
        _task(id="t2", needs=["t1"], surfaces=["graphs/delivery/b.py"]),
    ]
    tree = [
        {"path": "graphs/delivery/a.py", "repo": "graphs", "imports": ["graphs/delivery/b.py"]},
        {"path": "graphs/delivery/b.py", "repo": "graphs"},
    ]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_shared_test_file_ordered_by_needs_is_not_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", needs=["t1"], surfaces=["tests/test_shared.py"]),
    ]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_the_same_pair_with_the_needs_edge_removed_is_a_coupling_problem():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", surfaces=["tests/test_shared.py"]),
    ]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert len(problems) == 1
    assert problems[0].rule == "coupling"


def test_a_needs_chain_serializes_every_pair_transitively():
    tasks = [
        _task(id="t1", surfaces=["tests/test_shared.py"]),
        _task(id="t2", needs=["t1"], surfaces=["tests/test_shared.py"]),
        _task(id="t3", needs=["t2"], surfaces=["tests/test_shared.py"]),
    ]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_problem_severity_distinguishes_refusal_from_advisory():
    assert Problem("t1", "reach", "d", "f").severity == "refusal"
    assert Problem("t1", "coupling", "d", "f").severity == "refusal"
    assert Problem("t1", "grant", "d", "f").severity == "advisory"
    assert Problem("t1", "size", "d", "f").severity == "advisory"
