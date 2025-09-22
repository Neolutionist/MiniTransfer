#!/usr/bin/env python3
import sqlite3
import json
import sys
import csv
from pathlib import Path

DB_PATH = "/var/data/files_multi.db"

def list_tables(cur):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    return [r[0] for r in cur.fetchall()]

def get_columns(cur, table):
    cur.execute(f"PRAGMA table_info({table})")
    return [c[1] for c in cur.fetchall()]

def fetch_rows(cur, table, limit=None):
    if limit and int(limit) > 0:
        cur.execute(f"SELECT * FROM {table} LIMIT ?", (int(limit),))
    else:
        cur.execute(f"SELECT * FROM {table}")
    return cur.fetchall()

def show_schema_and_data(table_filter=None, limit=5):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    result = {}
    tables = list_tables(cur)

    if table_filter and table_filter not in tables:
        conn.close()
        print(f"⚠️  Tabel '{table_filter}' bestaat niet. Beschikbaar: {tables}")
        return

    for table in tables:
        if table_filter and table != table_filter:
            continue

        cols = get_columns(cur, table)
        rows = fetch_rows(cur, table, limit)

        result[table] = {
            "columns": cols,
            "sample_rows": rows
        }

    conn.close()
    print("📊 Database overzicht:")
    print(json.dumps(result, indent=2, default=str))

def export_csv(table, out_path, limit=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    tables = list_tables(cur)
    if table not in tables:
        conn.close()
        print(f"⚠️  Tabel '{table}' bestaat niet. Beschikbaar: {tables}")
        return

    cols = get_columns(cur, table)
    rows = fetch_rows(cur, table, limit)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow(r)

    conn.close()
    print(f"✅ CSV geëxporteerd: {out.resolve()}  (tabel: {table}, rijen: {len(rows)})")

def usage():
    print(
        """
Gebruik:
  # Toon alle tabellen met 5 voorbeeldrijen
  python3 debug_db.py

  # Toon alleen 'subscriptions' met 10 voorbeeldrijen
  python3 debug_db.py subscriptions --limit 10

  # Exporteer volledige tabel 'items' naar CSV
  python3 debug_db.py items --csv /var/data/items.csv

  # Exporteer met limiet
  python3 debug_db.py packages --csv /var/data/packages.csv --limit 100
"""
    )

if __name__ == "__main__":
    # eenvoudige argument parsing
    args = sys.argv[1:]
    if not args:
        show_schema_and_data()
        sys.exit(0)

    table = None
    csv_path = None
    limit = None

    # eerste arg mag een tabelnaam zijn (geen flag)
    if args and not args[0].startswith("--"):
        table = args[0]
        args = args[1:]

    # flags
    it = iter(args)
    for a in it:
        if a == "--csv":
            csv_path = next(it, None)
        elif a == "--limit":
            limit = next(it, None)
        elif a in {"-h", "--help"}:
            usage()
            sys.exit(0)
        else:
            print(f"Onbekende optie: {a}")
            usage()
            sys.exit(1)

    if csv_path:
        if not table:
            print("⚠️  Voor --csv moet je een tabelnaam opgeven.")
            usage()
            sys.exit(1)
        export_csv(table, csv_path, limit)
    else:
        show_schema_and_data(table_filter=table, limit=int(limit) if limit else 5)
