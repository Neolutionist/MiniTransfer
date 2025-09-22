import sqlite3

conn = sqlite3.connect("/var/data/files_multi.db")
cur = conn.cursor()

# toon alle tabellen
cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
print("Tabellen:", cur.fetchall())

# toon kolommen van subscriptions
cur.execute("PRAGMA table_info(subscriptions);")
print("Subscriptions kolommen:", cur.fetchall())

conn.close()
