from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate, open_store

NOW1 = "2026-09-25T00:00:00Z"
NOW2 = "2026-09-26T00:00:00Z"

COLUMNS = ["name", "ssh", "capacity", "state", "beat_at", "versions_json", "updated_at", "updated_by"]


# SQLite only: PRAGMA table_info has no Postgres counterpart.
def test_a_fresh_store_has_hosts_with_the_eight_columns():
    c = open_store("sqlite:///:memory:", NOW1)
    try:
        rows = c.query_all("PRAGMA table_info(hosts)")
        assert [(r[1], r[2]) for r in rows] == [
            ("name", "TEXT"),
            ("ssh", "TEXT"),
            ("capacity", "INTEGER"),
            ("state", "TEXT"),
            ("beat_at", "TEXT"),
            ("versions_json", "TEXT"),
            ("updated_at", "TEXT"),
            ("updated_by", "TEXT"),
        ]
        assert [r[1] for r in rows if r[5]] == ["name"]
    finally:
        c.close()


def test_a_database_at_version_seven_gains_hosts_and_keeps_chair_actions():
    c = connect("sqlite:///:memory:")
    try:
        assert migrate(c, NOW1, default_modules()[:7]) == 7
        before = c.query_all("PRAGMA table_info(chair_actions)")
        assert c.query_all("SELECT name FROM sqlite_master WHERE name = 'hosts'") == []
        assert migrate(c, NOW2, default_modules()) == 10
        assert c.query_all("SELECT version, applied_at FROM schema_version ORDER BY version")[7:] == [
            (8, NOW2), (9, NOW2), (10, NOW2),
        ]
        assert [r[1] for r in c.query_all("PRAGMA table_info(hosts)")] == COLUMNS
        assert c.query_all("SELECT COUNT(*) FROM hosts") == [(0,)]
        assert c.query_all("PRAGMA table_info(chair_actions)") == before
    finally:
        c.close()
