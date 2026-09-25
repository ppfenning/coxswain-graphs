from harness.store_dialect import upsert


def test_upsert_overwrites_the_non_key_column_on_the_same_key(store_conn):
    store_conn.execute("CREATE TABLE scratch_upsert (id INTEGER PRIMARY KEY, val TEXT)")
    sql = upsert(store_conn.dialect, "scratch_upsert", ["id", "val"], ["id"])
    store_conn.execute(sql, (1, "old"))
    store_conn.execute(sql, (1, "new"))
    assert store_conn.query_all("SELECT id, val FROM scratch_upsert") == [(1, "new")]
