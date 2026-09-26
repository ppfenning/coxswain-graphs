from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate, open_store

NOW1 = "2026-09-24T00:00:00Z"
NOW2 = "2026-09-25T00:00:00Z"

COLUMNS = ["ts", "epoch", "holder", "kind", "target", "status", "reason", "action_json"]


# SQLite only: PRAGMA table_info has no Postgres counterpart.
def test_a_fresh_store_has_chair_actions_with_the_eight_columns():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        rows = c.query_all("PRAGMA table_info(chair_actions)")
        assert [(r[1], r[2]) for r in rows] == [
            ("ts", "TEXT"),
            ("epoch", "INTEGER"),
            ("holder", "TEXT"),
            ("kind", "TEXT"),
            ("target", "TEXT"),
            ("status", "TEXT"),
            ("reason", "TEXT"),
            ("action_json", "TEXT"),
        ]
        assert [r[1] for r in sorted((r for r in rows if r[5]), key=lambda r: r[5])] == ["ts", "holder", "kind", "target"]
    finally:
        c.close()


def test_a_database_at_version_six_gains_chair_actions_and_keeps_work_items():
    c = connect("sqlite:///:memory:")
    try:
        assert migrate(c, NOW1, default_modules()[:6]) == 6
        c.execute(
            "INSERT INTO work_items (initiative, task_id, phase, state, needs_json, updated_at, updated_by)"
            " VALUES ('i1', 't1', 'p1', 'ready', '[]', ?, 'chair')",
            (NOW1,),
        )
        before = c.query_all("PRAGMA table_info(work_items)")
        assert c.query_all("SELECT name FROM sqlite_master WHERE name = 'chair_actions'") == []
        assert migrate(c, NOW2, default_modules()) == 7
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[6:] == [(7, NOW2)]
        assert [r[1] for r in c.query_all("PRAGMA table_info(chair_actions)")] == COLUMNS
        assert c.query_all("SELECT COUNT(*) FROM chair_actions") == [(0,)]
        assert c.query_all("PRAGMA table_info(work_items)") == before
        assert c.query_all("SELECT initiative, task_id, state FROM work_items") == [("i1", "t1", "ready")]
    finally:
        c.close()
