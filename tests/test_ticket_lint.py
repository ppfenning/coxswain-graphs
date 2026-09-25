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


def test_a_bare_slash_between_words_is_not_a_reach_problem():
    """tools-schema-version-1: "cartridges 1.0 / graphs 1.0" was refused as `names /, not inside`."""
    tasks = [_task(body="doctor prints `schema ok cartridges 1.0 / graphs 1.0 / tools 1.0`")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_path_shape_with_a_placeholder_is_notation_not_a_reach_problem():
    """tools-clean-guard-1..3, graphs-gate-landing-1: the seat wrote `runs/<run>/tasks/<phase>/<task>.json`
    to describe the record's shape and the corpus rule refused the whole decompose."""
    tasks = [_task(body="the record under `runs/<run>/tasks/<phase>/<task>.json` says landed")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_device_path_is_notation_not_a_reach_problem():
    """tools-chair-beater-1: "stdio to `/dev/null`" was refused as `names /dev/null, not inside`."""
    tasks = [_task(body="spawn the beater with `setsid`, stdio to `/dev/null`, its own process group")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_url_route_is_notation_not_a_reach_problem():
    """graphs-openai-compatible-runner-1: `/v1/chat/completions` was refused as `names /v1/chat/completions, not inside`."""
    tasks = [_task(body="POST to `{base}` + `/v1/chat/completions`; Jira lists at /rest/api/2/search")]
    assert lint_tickets(tasks, [], [], "graphs") == []


def test_a_rooted_path_under_a_real_filesystem_root_is_still_a_reach_refusal():
    tasks = [_task(body="read /etc/coxswain/settings.yaml and /tmp/notes.md")]
    assert [p.rule for p in lint_tickets(tasks, [], [], "graphs")] == ["reach", "reach"]


def test_an_absolute_spelling_of_a_repository_file_is_not_a_reach_problem():
    """graphs-advisory-not-surface-1: the seat wrote the repo file's absolute path and was refused."""
    tasks = [_task(body="the emit step in /home/x/repos/coxswain-graphs/graphs/delivery/ticket_lint.py merges advisories")]
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_a_real_corpus_path_is_still_a_reach_refusal():
    tasks = [_task(body="read `runs/tools-loop-fixes-3.log` for the outcome")]
    problems = lint_tickets(tasks, [], [], "graphs")
    assert [p.rule for p in problems] == ["reach"]


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
    assert Problem("t1", "cross_repo", "d", "f").severity == "advisory"


_TOOLS_TREE = [{"path": "agent_tools/run_store.py", "repo": "tools"}]


def test_a_tools_ticket_naming_a_graphs_file_gets_one_cross_repo_advisory():
    problems = lint_tickets([_task(body="edit harness/cli.py to add the flag")], _TOOLS_TREE, [], "tools")
    assert [(p.rule, p.severity, p.detail) for p in problems] == [
        ("cross_repo", "advisory", "names harness/cli.py, which lives in graphs")
    ]


def test_a_tools_ticket_naming_its_own_package_gets_no_cross_repo_advisory():
    assert lint_tickets([_task(body="edit agent_tools/run_store.py")], _TOOLS_TREE, [], "tools") == []


def test_docs_and_tests_paths_never_count_as_cross_repo():
    tasks = [_task(body="see tests/test_x.py and docs/x.md", surfaces=["tests/test_x.py", "docs/x.md"])]
    assert lint_tickets(tasks, _TOOLS_TREE, [], "tools") == []


def test_a_backticked_comma_terminated_token_still_names_the_other_repo():
    problems = lint_tickets([_task(body="change `runner/protocol.py`, then stop")], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == ["names runner/protocol.py, which lives in graphs"]


def test_a_surface_naming_another_repo_is_flagged_once():
    problems = lint_tickets([_task(body="core/x.py", surfaces=["core/x.py"])], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == ["names core/x.py, which lives in cartridges"]


def test_a_graphs_ticket_naming_a_new_file_under_its_own_roots_gets_no_cross_repo_advisory():
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    tasks = [_task(body="create harness/x.py and runner/y.py", surfaces=["graphs/ops/z.py"])]
    assert lint_tickets(tasks, tree, [], "graphs") == []


def test_the_target_repo_name_alone_marks_its_roots_as_own():
    tasks = [_task(body="create harness/x.py; see agent_tools/a.py")]
    assert [p.detail for p in lint_tickets(tasks, [], [], "~/repos/coxswain-graphs")] == [
        "names agent_tools/a.py, which lives in tools"
    ]


def test_an_unknown_target_with_no_tree_is_silent():
    assert lint_tickets([_task(body="edit harness/cli.py")], [], [], "") == []


def test_a_trailing_colon_or_quote_is_stripped_from_the_named_path():
    problems = lint_tickets([_task(body='see harness/cli.py: and "runner/protocol.py"')], _TOOLS_TREE, [], "tools")
    assert [p.detail for p in problems] == [
        "names harness/cli.py, which lives in graphs",
        "names runner/protocol.py, which lives in graphs",
    ]


def test_a_cross_repo_root_the_tree_lists_is_the_repositorys_own():
    tree = [{"path": "graphs/delivery/ticket_lint.py", "repo": "graphs"}]
    assert lint_tickets([_task(body="edit graphs/delivery/ticket_lint.py")], tree, [], "graphs") == []
