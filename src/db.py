"""Shared DuckDB connection factory with SQLite Row-compatible access."""
import duckdb
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "platform.duckdb"


class _Row(dict):
    """Dict that also supports integer-index access (used by COUNT(*)[0] callers)."""
    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self._values = values

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class _Result:
    def __init__(self, result):
        self._r = result
        self._cols = [d[0] for d in result.description] if result.description else []

    def fetchone(self):
        row = self._r.fetchone()
        return _Row(self._cols, row) if row else None

    def fetchall(self):
        return [_Row(self._cols, row) for row in self._r.fetchall()]

    def __iter__(self):
        for row in self._r.fetchall():
            yield _Row(self._cols, row)

    @property
    def rowcount(self):
        return self._r.rowcount


class _Conn:
    """DuckDB connection with the sqlite3 interface (execute/cursor/commit/close)."""
    def __init__(self, conn):
        self._c = conn

    def execute(self, query, params=None):
        r = self._c.execute(query, params if params is not None else [])
        return _Result(r)

    def executemany(self, query, params):
        self._c.executemany(query, params)

    def cursor(self):
        return self  # sqlite3 cursor() compatibility

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


def get_conn() -> _Conn:
    return _Conn(duckdb.connect(str(DB_PATH)))
