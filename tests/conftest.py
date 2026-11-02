import duckdb
import pytest

from spend_desk.generate.build import DUCKDB_PATH


@pytest.fixture(scope="session")
def db():
    """Local DuckDB mirror with RAW, CORE, and MART built.

    Generates RAW on first use if `make data` has not been run, then rebuilds the models so
    tests always see the current SQL.
    """
    if not DUCKDB_PATH.exists():
        from spend_desk.generate.build import main

        main()
    from spend_desk.models import Target, build

    con = duckdb.connect(str(DUCKDB_PATH))
    build(Target("duckdb", con), verbose=False)
    yield con
    con.close()
