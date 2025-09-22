#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Voeg een testpakket met één bestand toe aan de database.
Gebruik dit om de cleanup en debug scripts te testen.
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
import uuid

DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
DB_PATH = os.path.join(DATA_DIR, "files_multi.db")

def main():
    if not os.path.exists(DB_PATH):
        print(f"🚫 Database niet gevonden op: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)

    # Unieke waarden
    token = str(uuid.uuid4())[:8]
    tenant_id = "test_tenant"
    title = "Testpakket"
    created_at = datetime.now(timezone.utc).isoformat()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()

    # Voeg pakket toe
    conn.execute("""
        INSERT INTO packages (token, expires_at, password_hash, created_at, title, tenant_id)
        VALUES (?, ?, NULL, ?, ?, ?)
    """, (token, expires_at, created_at, title, tenant_id))

    # Voeg een bestand toe
    conn.execute("""
        INSERT INTO items (token, s3_key, name, path, size_bytes, tenant_id)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        token,
        f"{tenant_id}/{token}/dummy.txt",
        "dummy.txt",
        "",
        1234,  # bestandsgrootte in bytes
        tenant_id
    ))

    conn.commit()
    conn.close()

    print("✅ Testpakket toegevoegd")
    print(f"Token      : {token}")
    print(f"Tenant ID  : {tenant_id}")
    print(f"Vervalt op : {expires_at}")

if __name__ == "__main__":
    main()
