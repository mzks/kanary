import argparse
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
import time

VALID_NAMES = {"value1", "value2", "value3"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Emulate a SQLite data source for KANARY development")
    parser.add_argument("--db", help="SQLite file path. Defaults to KANARY_SQLITE_PATH or dev_data.db.")
    parser.add_argument(
        "--init",
        action="store_true",
        help="Create and seed the dev_samples table.",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="NAME=VALUE[@INTERVAL]",
        help="Append one or more values. INTERVAL is optional and enables repeated writes per value.",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all rows from dev_samples before writing.",
    )
    args = parser.parse_args()

    db_path = Path(args.db or os.environ.get("KANARY_SQLITE_PATH", "dev_data.db"))
    writes = parse_writes(args.set)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    try:
        ensure_schema(cur)

        if args.init:
            initialize_database(cur)
            conn.commit()

        if args.clear:
            cur.execute("DELETE FROM dev_samples")
            conn.commit()

        if not writes:
            print(db_path)
            return

        oneshot_writes = [(item["name"], item["value"]) for item in writes if item["interval"] is None]
        repeating_writes = [item for item in writes if item["interval"] is not None]

        if oneshot_writes:
            write_rows(cur, oneshot_writes)
            conn.commit()
        if not repeating_writes:
            print(db_path)
            return

        try:
            last_written_at = {index: 0.0 for index, _ in enumerate(repeating_writes)}
            while True:
                now_monotonic = time.monotonic()
                due_writes: list[tuple[str, float]] = []
                for index, item in enumerate(repeating_writes):
                    interval = item["interval"]
                    assert interval is not None
                    if now_monotonic - last_written_at[index] >= interval:
                        due_writes.append((item["name"], item["value"]))
                        last_written_at[index] = now_monotonic

                if due_writes:
                    write_rows(cur, due_writes)
                    conn.commit()

                time.sleep(0.2)
        except KeyboardInterrupt:
            return
    finally:
        conn.close()


def initialize_database(cur: sqlite3.Cursor) -> None:
    cur.execute("DROP TABLE IF EXISTS dev_samples")
    ensure_schema(cur)

    now = datetime.now(timezone.utc)
    rows = [
        (now.isoformat(), "value1", 12.5),
        (now.isoformat(), "value2", 105.0),
        (now.isoformat(), "value3", 0.45),
        ((now - timedelta(seconds=30)).isoformat(), "value1", 12.0),
        ((now - timedelta(seconds=30)).isoformat(), "value2", 100.0),
        ((now - timedelta(seconds=30)).isoformat(), "value3", 0.40),
    ]
    cur.executemany("INSERT INTO dev_samples (ts, name, value) VALUES (?, ?, ?)", rows)


def ensure_schema(cur: sqlite3.Cursor) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS dev_samples (
            ts TEXT NOT NULL,
            name TEXT NOT NULL,
            value REAL NOT NULL
        )
        """
    )


def parse_writes(sets: list[str]) -> list[dict[str, float | str | None]]:
    writes: list[dict[str, float | str | None]] = []
    for item in sets:
        if "=" not in item:
            raise SystemExit(f"invalid --set value: {item!r}")

        item_name, raw_value = item.split("=", 1)
        if item_name not in VALID_NAMES:
            raise SystemExit(f"invalid name for --set: {item_name!r}")

        raw_interval: str | None = None
        if "@" in raw_value:
            raw_value, raw_interval = raw_value.rsplit("@", 1)

        try:
            item_value = float(raw_value)
        except ValueError as exc:
            raise SystemExit(f"invalid numeric value for --set: {item!r}") from exc

        interval: float | None = None
        if raw_interval is not None:
            try:
                interval = float(raw_interval)
            except ValueError as exc:
                raise SystemExit(f"invalid interval for --set: {item!r}") from exc
            if interval <= 0:
                raise SystemExit(f"interval must be positive for --set: {item!r}")

        writes.append(
            {
                "name": item_name,
                "value": item_value,
                "interval": interval,
            }
        )

    return writes


def write_rows(cur: sqlite3.Cursor, writes: list[tuple[str, float]]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    cur.executemany(
        "INSERT INTO dev_samples (ts, name, value) VALUES (?, ?, ?)",
        [(now, name, value) for name, value in writes],
    )


if __name__ == "__main__":
    main()
