#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
debug_db.py
-----------
Snelle inspectie van files_multi.db.

  # Toon alle tabellen met 5 voorbeeldrijen
  python3 debug_db.py

  # Toon alleen 'subscriptions' met 10 voorbeeldrijen
  python3 debug_db.py subscriptions --limit 10

  # Exporteer volledige tabel 'items' naar CSV
  python3 debug_db.py items --csv ./items.csv

  # Exporteer met limiet
  python3 debug_db.py packages --csv ./packages.csv --limit 100

  # Override DB-pad
  python3 debug_db.py --db /pad/naar/files_multi.db

Pad-resolutie:
  1) --db argument (hoogste prioriteit)
  2) DATA_DIR env var → <DATA_DIR>/files_multi.db
  3) Fallback: <scriptdir>/data/files_multi.db
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sqlite3
import sys
from pathlib import Path


def resolve_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return (Path(data_dir) / "files_multi.db").resolve()
    return (Path(__file__).resolve().parent / "data" / "files_multi.db").resolve()


def list_tables(cur: sqlite3.Cursor) -> list[str]:
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
    return [r[0] for r in cur.fetchall()]


def get_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    cur.execute(f"PRAGMA table_info({table})")
    return [c[1] for c in cur.fetchall()]


def fetch_rows(cur: sqlite3.Cursor, table: str, limit: int | None = None) -> list[tuple]:
    if limit and limit > 0:
        cur.execute(f"SELECT * FROM {table} LIMIT ?", (limit,))
    else:
        cur.execute(f"SELECT * FROM {table}")
    return cur.fetchall()


def show_schema_and_data(db_path: Path, table_filter: str | None = None, limit: int = 5) -> None:
    if not db_path.exists():
        print(f"🚫 Database niet gevonden op: {db_path}", file=sys.stderr)
        sys.exit(3)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        tables = list_tables(cur)

        if table_filter and table_filter not in tables:
            print(f"⚠️  Tabel '{table_filter}' bestaat niet. Beschikbaar: {tables}")
            return

        result: dict = {}
        for table in tables:
            if table_filter and table != table_filter:
                continue
            result[table] = {
                "columns": get_columns(cur, table),
                "sample_rows": fetch_rows(cur, table, limit),
            }
    finally:
        conn.close()

    print(f"📊 Database overzicht ({db_path}):")
    print(json.dumps(result, indent=2, default=str, ensure_ascii=False))


def export_csv(db_path: Path, table: str, out_path: str, limit: int | None = None) -> None:
    if not db_path.exists():
        print(f"🚫 Database niet gevonden op: {db_path}", file=sys.stderr)
        sys.exit(3)

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        tables = list_tables(cur)
        if table not in tables:
            print(f"⚠️  Tabel '{table}' bestaat niet. Beschikbaar: {tables}")
            return

        cols = get_columns(cur, table)
        rows = fetch_rows(cur, table, limit)
    finally:
        conn.close()

    out = Path(out_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)

    print(f"✅ CSV geëxporteerd: {out}  (tabel: {table}, rijen: {len(rows)})")


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Snelle inspectie van files_multi.db",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("table", nargs="?", default=None, help="Optionele tabelnaam.")
    ap.add_argument("--csv", default=None, metavar="PATH", help="Exporteer tabel naar CSV.")
    ap.add_argument("--limit", type=int, default=None, help="Maximum aantal rijen.")
    ap.add_argument("--db", default=None, help="Override pad naar files_multi.db.")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    db_path = resolve_db_path(args.db)

    if args.csv:
        if not args.table:
            print("⚠️  Voor --csv moet je een tabelnaam opgeven.", file=sys.stderr)
            sys.exit(1)
        export_csv(db_path, args.table, args.csv, args.limit)
    else:
        show_schema_and_data(db_path, table_filter=args.table, limit=args.limit or 5)


if __name__ == "__main__":
    main()
