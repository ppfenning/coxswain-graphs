import pytest
from conftest import with_search_path


def test_with_search_path_appends_options_and_keeps_an_existing_query():
    assert with_search_path("postgresql://u:p@h:5432/db", "t_x") == "postgresql://u:p@h:5432/db?options=-csearch_path%3Dt_x"
    assert (
        with_search_path("postgresql://h/db?sslmode=disable", "t_x")
        == "postgresql://h/db?sslmode=disable&options=-csearch_path%3Dt_x"
    )


def test_libpq_decodes_the_search_path_option():
    conninfo = pytest.importorskip("psycopg.conninfo")
    parsed = conninfo.conninfo_to_dict(with_search_path("postgresql://u:p@h:5432/db", "t_x"))
    assert parsed["options"] == "-csearch_path=t_x"
