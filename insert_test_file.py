#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Voeg een testpakket toe met een vervaldatum in het verleden.
Gebruik dit om cleanup te testen.
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
    title = "ExpiredTest"
    created_at = datetime.now(timezone.utc).isoformat()
    # Vervaldatum 1 dag geleden
    expires_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

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
        f"{tenant_id}/{token}/expired_dummy.txt",
        "expired_dummy.txt",
        "",
        5678,  # bestandsgrootte in bytes
        tenant_id
    ))

    conn.commit()
    conn.close()

    print("✅ Expired testpakket toegevoegd")
    print(f"Token      : {token}")
    print(f"Tenant ID  : {tenant_id}")
    print(f"Verviel op : {expires_at}")

if __name__ == "__main__":
    main()
