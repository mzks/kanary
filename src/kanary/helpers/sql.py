"""SQL sources for the two common table shapes.

Wide format - one row per time, one column per quantity:

    observed_at          | temperature | humidity
    2026-11-18 10:00:00  | 21.5        | 40.1

Long format - one row per quantity:

    observed_at          | name        | value
    2026-11-18 10:00:00  | temperature | 21.5
    2026-11-18 10:00:00  | humidity    | 40.1

Usually only settings are needed: database_type, database (and host, user,
password for a server), table, and the column names. The connection and
the SELECT are built from them. To read something else, set ``query`` (it is
then used as written, with ``params``) or override ``connect()`` for another
driver.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
import re
from typing import Any

from ..models import PollResult
from ..plugin_files import path_beside_plugin
from ..source import Source, inputs, no_data
from ..values import to_datetime, to_number, to_timezone

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
_PLACEHOLDER = {"sqlite": "?", "postgresql": "%s", "mysql": "%s"}


class SQLSource(Source):
    """Shared connection handling; use SQLWideSource or SQLLongSource."""

    # Connection, used by the default connect().
    database_type: str = "sqlite"  # "sqlite", "postgresql", or "mysql"
    database: str = ""  # sqlite: file path (relative = next to the plugin); others: database name
    host: str = "localhost"
    port: int | None = None
    user: str | None = None
    password: str | None = None  # a secret: password = os.environ.get("DB_PASSWORD")
    timeout: float = 5.0  # connect and statement timeout, seconds

    # What to read. table and the column settings build the SELECT; a non-empty
    # query is used instead, as written.
    table: str = ""
    timestamp_column: str | None = None
    query: str = ""
    params: tuple[Any, ...] | dict[str, Any] = ()
    # Timestamps without a timezone (e.g. PostgreSQL "timestamp without time zone",
    # SQLite text) are read in this zone ("UTC", "Asia/Tokyo", "+09:00");
    # None means this host's local time.
    timestamp_timezone: str | None = None

    def connect(self) -> Any:
        """Open a DB-API connection from the settings, with timeouts and autocommit."""
        kind = self._database_type()
        if not self.database:
            raise ValueError(f"{type(self).__name__}.database is empty")
        password = self.password
        if kind == "sqlite":
            import sqlite3

            path = self.database if self.database == ":memory:" else path_beside_plugin(self, self.database)
            return sqlite3.connect(path, timeout=self.timeout, check_same_thread=False, isolation_level=None)
        if kind == "postgresql":
            psycopg = _import("psycopg", "pip install 'psycopg[binary]'")
            return psycopg.connect(
                host=self.host,
                port=self.port or 5432,
                dbname=self.database,
                user=self.user,
                password=password,
                connect_timeout=max(1, int(self.timeout)),
                autocommit=True,
                options=f"-c statement_timeout={int(self.timeout * 1000)}",
            )
        pymysql = _import("pymysql", "pip install pymysql")
        return pymysql.connect(
            host=self.host,
            port=self.port or 3306,
            database=self.database,
            user=self.user,
            password=password or "",
            connect_timeout=self.timeout,
            read_timeout=self.timeout,
            autocommit=True,
        )

    def build_query(self) -> tuple[str, tuple[Any, ...]]:
        """The SELECT built from the settings, and its parameters."""
        raise NotImplementedError

    def init(self) -> None:
        self.conn = self.connect()

    def terminate(self) -> None:
        conn = getattr(self, "conn", None)
        if conn is not None:
            conn.close()

    def fetch_rows(self) -> list[dict[str, Any]]:
        if self.query.strip():
            query, params = self.query, self.params
        else:
            query, params = self.build_query()
        cursor = self.conn.cursor()
        try:
            # Without params, '%' in the query (e.g. LIKE 'a%') is left alone by drivers such as psycopg.
            if params:
                cursor.execute(query, params)
            else:
                cursor.execute(query)
            columns = [column[0] for column in cursor.description or ()]
            # Rows may be tuples or, with a dict row factory, mappings already.
            return [dict(row) if isinstance(row, Mapping) else dict(zip(columns, row)) for row in cursor.fetchall()]
        finally:
            cursor.close()

    def row_timestamp(self, row: dict[str, Any]) -> Any:
        if not self.timestamp_column:
            return None
        return to_datetime(row.get(self.timestamp_column), naive_timezone=self.timestamp_timezone)

    def quote(self, identifier: str) -> str:
        """Quote a table or column name ("schema.table" allowed) for this database type."""
        if not isinstance(identifier, str) or not _IDENTIFIER.match(identifier):
            raise ValueError(f"{identifier!r} is not a plain table or column name; write query instead")
        mark = "`" if self._database_type() == "mysql" else '"'
        return ".".join(f"{mark}{part}{mark}" for part in identifier.split("."))

    def _database_type(self) -> str:
        kind = str(self.database_type).strip().lower()
        kind = {"postgres": "postgresql", "sqlite3": "sqlite", "mariadb": "mysql"}.get(kind, kind)
        if kind not in _PLACEHOLDER:
            raise ValueError(f"database_type must be 'sqlite', 'postgresql', or 'mysql', not {self.database_type!r}")
        return kind

    def _require_table(self) -> str:
        if not self.table:
            raise ValueError(f"{type(self).__name__} needs table (or a query)")
        if not self.timestamp_column:
            raise ValueError(f"{type(self).__name__} needs timestamp_column to find the newest rows (or a query)")
        return self.quote(self.table)


class SQLWideSource(SQLSource):
    """Read the newest row; each column becomes an input.

    columns maps input names to column names; when empty, every column except
    timestamp_column is used under its own name. Non-numeric values go to
    metadata["text"].
    """

    columns: dict[str, str] = {}

    def build_query(self) -> tuple[str, tuple[Any, ...]]:
        table = self._require_table()
        wanted = [self.timestamp_column, *self.columns.values()] if self.columns else []
        selected = ", ".join(self.quote(column) for column in dict.fromkeys(wanted)) or "*"
        return f"SELECT {selected} FROM {table} ORDER BY {self.quote(self.timestamp_column)} DESC LIMIT 1", ()

    def poll(self) -> PollResult:
        rows = self.fetch_rows()
        if not rows:
            return no_data(reason="query returned no rows")
        row = rows[0]
        timestamp = self.row_timestamp(row)
        columns = self.columns or {name: name for name in row if name != self.timestamp_column}
        items, texts = [], {}
        for input_name, column in columns.items():
            value = row.get(column)
            number = to_number(value)
            if number is None:
                texts[input_name] = None if value is None else str(value)
                continue
            items.append((input_name, number, timestamp, {"column": column}))
        return inputs(items, metadata={"text": texts} if texts else None)


class SQLLongSource(SQLSource):
    """Read the newest row of every name; each name becomes an input.

    names maps upstream names to input names; when given, other names are
    ignored. lookback limits the rows scanned to recent ones, which
    keeps the query cheap on a large table. The upstream name is kept in each
    input's metadata.
    """

    name_column: str = "name"
    value_column: str = "value"
    names: dict[str, str] = {}
    lookback: float | None = None

    def build_query(self) -> tuple[str, tuple[Any, ...]]:
        table = self._require_table()
        name, value, ts = (self.quote(c) for c in (self.name_column, self.value_column, self.timestamp_column))
        where, params = "", ()
        if self.lookback:
            where = f" WHERE {ts} >= {_PLACEHOLDER[self._database_type()]}"
            params = (self._since(),)
        query = (
            f"SELECT t.{name}, t.{value}, t.{ts} FROM {table} AS t"
            f" JOIN (SELECT {name}, MAX({ts}) AS latest FROM {table}{where} GROUP BY {name}) AS m"
            f" ON t.{name} = m.{name} AND t.{ts} = m.latest"
        )
        return query, params

    def poll(self) -> PollResult:
        rows = self.fetch_rows()
        if not rows:
            return no_data(reason="query returned no rows")
        items, texts, seen = [], {}, set()
        for row in rows:
            upstream = str(row.get(self.name_column))
            if self.names and upstream not in self.names:
                continue
            input_name = self.names.get(upstream, upstream)
            if input_name in seen:
                continue
            seen.add(input_name)
            value = row.get(self.value_column)
            number = to_number(value)
            if number is None:
                texts[input_name] = None if value is None else str(value)
                continue
            items.append((input_name, number, self.row_timestamp(row), {"name": upstream}))
        return inputs(items, metadata={"text": texts} if texts else None)

    def _since(self) -> Any:
        # Compare in the zone the timestamps are stored in, as a naive value.
        now = datetime.now(to_timezone(self.timestamp_timezone)) if self.timestamp_timezone else datetime.now()
        since = (now - timedelta(seconds=self.lookback or 0)).replace(tzinfo=None)
        if self._database_type() == "sqlite":
            return since.isoformat(sep=" ", timespec="seconds")  # how SQLite stores datetime text
        return since


def _import(module: str, hint: str) -> Any:
    try:
        return __import__(module)
    except ImportError:
        raise RuntimeError(
            f"the {module} package is needed for this database_type ({hint}): install it into the Python "
            "environment that runs KANARY (see docs/deployment.md: When A Plugin Needs Additional Python Modules)"
        ) from None
