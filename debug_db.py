import sqlite3
import json

DB_PATH = "/var/data/files_multi.db"

def show_schema_and_data():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    result = {}

    # Pak alle tabellen
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [r[0] for r in cur.fetchall()]

    for table in tables:
        # Haal kolommen op
        cur.execute(f"PRAGMA table_info({table})")
        cols = [c[1] for c in cur.fetchall()]

        # Haal de eerste 5 rijen data op
        cur.execute(f"SELECT * FROM {table} LIMIT 5")
        rows = cur.fetchall()

        result[table] = {
            "columns": cols,
            "sample_rows": rows
        }

    conn.close()

    print("📊 Database overzicht:")
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    show_schema_and_data()
