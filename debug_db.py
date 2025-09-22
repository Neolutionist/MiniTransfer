import sqlite3
import json

DB_PATH = "/var/data/files_multi.db"

def main():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # alle tabellen ophalen
        cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
        tables = [row[0] for row in cur.fetchall()]
        print("📦 Tabellen gevonden:", tables)

        schema = {}
        for table in tables:
            cur.execute(f"PRAGMA table_info({table});")
            cols = [r[1] for r in cur.fetchall()]  # kolomnamen
            schema[table] = cols

        print("\n📑 Schema overzicht:")
        print(json.dumps(schema, indent=2))

        conn.close()

    except Exception as e:
        print("❌ Fout bij openen database:", e)


if __name__ == "__main__":
    main()
