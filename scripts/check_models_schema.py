"""Compare coverai/models.py against a real SQLite database, column by column.

Usage:
    python3 scripts/check_models_schema.py            # compare against coverai.db
    python3 scripts/check_models_schema.py --fresh    # compare against a fresh
                                                      # db built by CoverAiStore
    python3 scripts/check_models_schema.py --db path/to/other.db

For each table in the models it checks that the database has the same
columns with the same type affinity, NOT NULL flag, and DEFAULT value.
Column order and foreign keys are not compared: SQLite's ALTER TABLE can
only append columns and cannot add foreign keys, so the live database
legitimately differs there. Exit code is 1 if any mismatch is found.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coverai.models import Base  # noqa: E402


def type_affinity(declared: str) -> str:
    """Collapse a declared column type to SQLite's storage affinity.

    SQLite doesn't enforce exact type names; 'VARCHAR' and 'TEXT' both store
    text. Comparing affinities avoids false alarms between the two.
    """
    declared = declared.upper()
    if "INT" in declared:
        return "INTEGER"
    if any(token in declared for token in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    return declared or "BLOB"


def model_default(column) -> str | None:
    """Render a model column's server_default the way PRAGMA reports it."""
    if column.server_default is None:
        return None
    arg = column.server_default.arg
    if isinstance(arg, str):
        return f"'{arg}'"
    return str(arg)


def db_columns(conn: sqlite3.Connection, table: str) -> dict[str, dict]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {
        str(row["name"]): {
            "affinity": type_affinity(str(row["type"])),
            "notnull": bool(row["notnull"]),
            "default": row["dflt_value"],
        }
        for row in rows
    }


def compare(db_path: Path) -> list[str]:
    problems: list[str] = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    for table in Base.metadata.sorted_tables:
        actual = db_columns(conn, table.name)
        if not actual:
            problems.append(f"{table.name}: table missing from database")
            continue
        for column in table.columns:
            info = actual.pop(column.name, None)
            if info is None:
                problems.append(f"{table.name}.{column.name}: column missing from database")
                continue
            expected_affinity = type_affinity(column.type.compile(dialect=sqlite3_dialect()))
            if info["affinity"] != expected_affinity:
                problems.append(
                    f"{table.name}.{column.name}: type {info['affinity']} in db, "
                    f"model expects {expected_affinity}"
                )
            expected_notnull = not column.nullable and not column.primary_key
            if info["notnull"] != expected_notnull and not column.primary_key:
                problems.append(
                    f"{table.name}.{column.name}: NOT NULL is {info['notnull']} in db, "
                    f"model expects {expected_notnull}"
                )
            expected_default = model_default(column)
            if (info["default"] or None) != expected_default and expected_default is not None:
                problems.append(
                    f"{table.name}.{column.name}: DEFAULT {info['default']!r} in db, "
                    f"model expects {expected_default!r}"
                )
        for leftover in actual:
            problems.append(f"{table.name}.{leftover}: column in database but not in models")
    conn.close()
    return problems


def sqlite3_dialect():
    from sqlalchemy.dialects import sqlite

    return sqlite.dialect()


def fresh_db_path(tmp_dir: str) -> Path:
    from coverai.storage import CoverAiStore

    path = Path(tmp_dir) / "fresh.db"
    CoverAiStore(path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="coverai.db", help="database file to compare against")
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="compare against a brand-new database created by CoverAiStore instead",
    )
    args = parser.parse_args()

    if args.fresh:
        with tempfile.TemporaryDirectory() as tmp_dir:
            problems = compare(fresh_db_path(tmp_dir))
            label = "fresh CoverAiStore database"
    else:
        db_path = Path(args.db)
        if not db_path.exists():
            print(f"error: {db_path} does not exist", file=sys.stderr)
            return 2
        problems = compare(db_path)
        label = str(db_path)

    if problems:
        print(f"{len(problems)} difference(s) between models and {label}:\n")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"models match {label}: all tables, columns, types, NOT NULLs, and defaults agree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
