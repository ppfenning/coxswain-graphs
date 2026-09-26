from harness.work_mirror import (
    AGREE,
    USE_FILE_AND_UPSERT,
    USE_STORE_REWRITE_FILE,
    authority_decision,
    check_expected_state,
    plan_mirror,
    set_frontmatter_state,
)

TICKET = "---\r\nid: t1\r\nstate: open\r\nphase: p1\r\n---\r\nBody.\r\nstate: not this one\r\n"


def test_no_row_uses_file_and_upserts_in_both_modes():
    assert authority_decision("files", "open", None) == USE_FILE_AND_UPSERT
    assert authority_decision("store", "open", None) == USE_FILE_AND_UPSERT


def test_equal_states_agree():
    assert authority_decision("store", "open", "open") == AGREE


def test_differing_states_under_files_upsert_from_the_file():
    assert authority_decision("files", "done", "open") == USE_FILE_AND_UPSERT


def test_differing_states_under_store_rewrite_the_file():
    assert authority_decision("store", "done", "open") == USE_STORE_REWRITE_FILE


def test_files_mode_upserts_exactly_when_plan_mirror_does_without_file_times():
    item = {"id": "t1", "phase": "p1", "state": "done", "needs": []}
    row = {"task_id": "t1", "phase": "p1", "needs": [], "updated_at": None}
    cases = [None, "done", "open"]
    stored = [[] if s is None else [{**row, "state": s}] for s in cases]
    mirror = [bool(plan_mirror("i", [item], rows, {}, "d")[0]) for rows in stored]
    decided = [authority_decision("files", "done", s) == USE_FILE_AND_UPSERT for s in cases]
    assert mirror == decided == [True, False, True]


def test_files_mode_upserts_exactly_when_plan_mirror_does_with_an_older_row():
    item = {"id": "t1", "phase": "p1", "state": "done", "needs": []}
    row = {"task_id": "t1", "phase": "p1", "state": "open", "needs": [], "updated_at": "2026-09-25T08:00:00Z"}
    upserts, _ = plan_mirror("i", [item], [row], {"t1": "2026-09-25T09:00:00Z"}, "d")
    assert (bool(upserts), authority_decision("files", "done", "open")) == (True, USE_FILE_AND_UPSERT)


def test_only_the_frontmatter_state_line_changes_and_crlf_and_body_survive():
    out = set_frontmatter_state(TICKET, "done")
    assert out == TICKET.replace("state: open", "state: done", 1)
    assert out.endswith("Body.\r\nstate: not this one\r\n")


def test_a_matching_state_returns_the_text_unchanged():
    assert set_frontmatter_state(TICKET, "open") == TICKET


def test_no_frontmatter_state_line_returns_none():
    assert set_frontmatter_state("---\nid: t1\n---\nstate: open\n", "done") is None


def test_no_frontmatter_at_all_returns_none():
    assert set_frontmatter_state("state: open\n", "done") is None


def test_a_missing_trailing_newline_stays_missing():
    assert set_frontmatter_state("---\nstate: open\n---", "done") == "---\nstate: done\n---"


def test_other_unicode_line_separators_in_the_state_line_and_body_survive():
    text = "---\nstate: open\x0b\n---\nbody\N{LINE SEPARATOR}state: x\x0c\n"
    assert set_frontmatter_state(text, "done") == "---\nstate: done\x0b\n---\nbody\N{LINE SEPARATOR}state: x\x0c\n"


def test_a_quoted_and_commented_matching_value_is_left_unchanged():
    text = '---\nstate: "open"  # set by cli\n---\n'
    assert set_frontmatter_state(text, "open") is text


def test_a_quoted_and_commented_value_keeps_its_quotes_and_comment():
    text = "---\r\nstate: 'open'  # cli\r\n---\r\n"
    assert set_frontmatter_state(text, "done") == "---\r\nstate: 'done'  # cli\r\n---\r\n"


def test_an_empty_state_value_is_filled():
    assert set_frontmatter_state("---\r\nstate:\r\n---\r\n", "done") == "---\r\nstate: done\r\n---\r\n"


def test_a_matching_state_is_allowed():
    assert check_expected_state("open", "open")[0] is True


def test_a_mismatch_is_refused_naming_both_states():
    assert check_expected_state("open", "done") == (False, "expected state 'open' but store has 'done'")


def test_a_missing_row_is_refused_as_no_row():
    assert check_expected_state("open", None) == (False, "expected state 'open' but store has no row")
