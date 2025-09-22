# insert_test_record.py
import sqlite3
from pathlib import Path

# Pad naar je database (zelfde als in je andere scripts)
DB_PATH = Path("/var/data/files_multi.db")

def main():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Simpele test insert (subscription met korte vervaltijd)
    cur.execute("""
        INSERT INTO subscriptions (login_email, plan_value, subscription_id, status, created_at, tenant_id)
        VALUES (?, ?, ?, ?, datetime('now'), ?)
    """, (
        "testuser@example.com",  # login_email
        "0.5",                   # plan_value (plan zoals in je PLAN_MAP)
        "test_sub_123",          # subscription_id (dummy)
        "active",                # status
        "test_tenant_001"        # tenant_id
    ))

    conn.commit()
    conn.close()
    print("✅ Test record toegevoegd aan subscriptions!")

if __name__ == "__main__":
    main()
