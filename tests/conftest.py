import duckdb
import pytest

from spend_desk.generate.build import DUCKDB_PATH


@pytest.fixture(scope="session")
def raw_db():
    """Local DuckDB mirror. Generated on first use if `make data` has not been run."""
    if not DUCKDB_PATH.exists():
        from spend_desk.generate.build import main

        main()
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    yield con
    con.close()
