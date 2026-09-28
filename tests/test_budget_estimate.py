from graphs.delivery.budget_estimate import estimate_budget


def test_repo_and_bucket_with_at_least_ten_rows_returns_their_p75_inside_the_clamp():
    history = [
        ("svc", 2, 1.00),
        ("svc", 2, 1.05),
        ("svc", 2, 1.10),
        ("svc", 2, 1.15),
        ("svc", 2, 1.20),
        ("svc", 2, 1.25),
        ("svc", 2, 1.30),
        ("svc", 2, 1.35),
        ("svc", 2, 1.40),
        ("svc", 2, 1.45),
        ("other-repo", 2, 9.00),
    ]
    assert estimate_budget(history, "svc", 2) == (1.34, "estimate")


def test_fewer_than_ten_rows_in_bucket_but_at_least_ten_at_repo_level_falls_back_to_repo_p75():
    history = [
        ("svc", 1, 2.00),
        ("svc", 1, 2.10),
        ("svc", 1, 2.20),
        ("svc", 2, 1.00),
        ("svc", 2, 1.10),
        ("svc", 2, 1.20),
        ("svc", 2, 1.30),
        ("svc", 2, 1.40),
        ("svc", 2, 1.50),
        ("svc", 2, 1.60),
        ("svc", 2, 1.70),
        ("svc", 2, 1.80),
    ]
    assert estimate_budget(history, "svc", 1) == (1.85, "estimate")


def test_fewer_than_ten_rows_at_every_level_returns_the_default():
    history = [
        ("svc", 1, 2.00),
        ("svc", 1, 2.10),
        ("svc", 1, 2.20),
        ("other-repo", 1, 3.00),
        ("other-repo", 1, 3.10),
        ("other-repo", 1, 3.20),
        ("other-repo", 1, 3.30),
        ("other-repo", 1, 3.40),
        ("other-repo", 1, 3.50),
        ("other-repo", 1, 3.60),
        ("other-repo", 1, 3.70),
        ("other-repo", 1, 3.80),
        ("other-repo", 1, 3.90),
    ]
    assert estimate_budget(history, "svc", 1) == (1.50, "estimate")


def test_a_p75_above_four_dollars_clamps_to_the_ceiling():
    history = [
        ("svc", 5, 4.50),
        ("svc", 5, 4.60),
        ("svc", 5, 4.70),
        ("svc", 5, 4.80),
        ("svc", 5, 4.90),
        ("svc", 5, 5.00),
        ("svc", 5, 5.10),
        ("svc", 5, 5.20),
        ("svc", 5, 5.30),
        ("svc", 5, 5.40),
    ]
    assert estimate_budget(history, "svc", 6) == (4.00, "estimate")
