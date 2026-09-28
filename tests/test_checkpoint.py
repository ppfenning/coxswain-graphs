from graphs.delivery.checkpoint import CheckpointSignals, Resume, Revise, checkpoint_decision

FRACTIONS = (1.0, 2.0)


def test_files_outside_surfaces_revises_with_the_first_path():
    signals = CheckpointSignals(
        spend_usd=1.0,
        guide_usd=2.0,
        checkpoint_index=0,
        turns=3,
        diff_grew=True,
        checks_pass=True,
        files_outside_surfaces=("harness/cli.py", "harness/cos.py"),
    )
    assert checkpoint_decision(signals, FRACTIONS) == Revise(
        "re-scope: partial work touches harness/cli.py outside the task's surfaces"
    )


def test_diff_did_not_grow_revises_with_re_ground():
    signals = CheckpointSignals(
        spend_usd=1.0,
        guide_usd=2.0,
        checkpoint_index=0,
        turns=3,
        diff_grew=False,
        checks_pass=True,
        files_outside_surfaces=(),
    )
    assert checkpoint_decision(signals, FRACTIONS) == Revise(
        "re-ground: no change since the previous checkpoint"
    )


def test_runaway_checkpoint_with_failing_checks_revises_with_split():
    signals = CheckpointSignals(
        spend_usd=1.9,
        guide_usd=1.0,
        checkpoint_index=1,
        turns=9,
        diff_grew=True,
        checks_pass=False,
        files_outside_surfaces=(),
    )
    assert checkpoint_decision(signals, FRACTIONS) == Revise(
        "split: runaway checkpoint reached with the named checks still failing"
    )


def test_otherwise_resumes():
    signals = CheckpointSignals(
        spend_usd=0.5,
        guide_usd=1.0,
        checkpoint_index=0,
        turns=3,
        diff_grew=True,
        checks_pass=False,
        files_outside_surfaces=(),
    )
    assert checkpoint_decision(signals, FRACTIONS) == Resume()
