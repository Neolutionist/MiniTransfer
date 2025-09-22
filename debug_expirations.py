#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Debug-tool: toon vervaldatums van pakketten en bijbehorende bestanden.

Gebruik:
  python3 debug_expirations.py
  python3 debug_expirations.py --details     # toont alle files per pakket
"""

import os
import sqlite3
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

# Locatie DB: zelfde logica als je app
DATA_DIR = os.environ.get("DATA_DIR", "/var/data")
DB_PATH  = os.path.join(DATA_DIR, "files_multi.db")


def human_bytes(n: int) -> str:
    x = float(n)
    for u in ["B", "KB", "MB", "GB", "TB"]:
        if x < 1024.0 or u == "TB":
            return (f"{x:.1f} {u}" if u != "B" else f"{int(x)} {u}")
        x /= 1024.0


def parse_iso(dt: str) -> datetime:
    # In je app wordt expires_at gezet met datetime.now(timezone.utc).isoformat()
    # Dat levert een string met timezone offset (bv. ...+00:00). fromisoformat kan dit direct parsen.
    return datetime.fromisoformat(dt)


def load_packages(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT token, title, tenant_id, created_at, expires_at
        FROM packages
        ORDER BY expires_at ASC
    """).fetchall()
    return [dict(r) for r in rows]


def package_stats(conn: sqlite3.Connection, token: str, tenant_id: str) -> Tuple[int, int]:
    """Aantal bestanden en totale grootte voor één pakket (token + tenant)."""
    row = conn.execute("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total
        FROM items
        WHERE token = ? AND tenant_id = ?
    """, (token, tenant_id)).fetchone()
    return (int(row[0] or 0), int(row[1] or 0))


def list_items(conn: sqlite3.Connection, token: str, tenant_id: str) -> List[Dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT name, path, size_bytes
        FROM items
        WHERE token = ? AND tenant_id = ?
        ORDER BY path, name
    """, (token, tenant_id)).fetchall()
    return [dict(r) for r in rows]


def main(show_details: bool = False) -> None:
    if not os.path.exists(DB_PATH):
        print(f"🚫 Database niet gevonden op: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)

    pkgs = load_packages(conn)
    if not pkgs:
        print("ℹ️  Geen pakketten gevonden.")
        return

    now = datetime.now(timezone.utc)
    print("\n📦 Overzicht vervaldata per pakket\n")

    for p in pkgs:
        token      = p["token"]
        title      = p.get("title") or ""
        tenant_id  = p.get("tenant_id") or ""
        created_at = p.get("created_at") or ""
        expires_at = p.get("expires_at") or ""

        # Bereken resterende tijd
        try:
            dt_exp = parse_iso(expires_at)
            delta  = dt_exp - now
            expired = delta.total_seconds() <= 0
            if expired:
                rem = "EXPIRED"
            else:
                days = int(delta.days)
                hrs  = int((delta.seconds // 3600) % 24)
                rem  = f"{days}d {hrs}h"
        except Exception:
            rem = "(ongeldige datum)"

        cnt, total = package_stats(conn, token, tenant_id)

        print("—"*70)
        print(f"Tenant       : {tenant_id}")
        print(f"Token        : {token}")
        print(f"Title        : {title}")
        print(f"Aangemaakt   : {created_at}")
        print(f"Vervalt op   : {expires_at}   (remaining: {rem})")
        print(f"Files        : {cnt}  (totaal {human_bytes(total)})")

        if show_details and cnt:
            items = list_items(conn, token, tenant_id)
            for it in items:
                name = it["name"]
                path = it["path"]
                size = human_bytes(int(it["size_bytes"] or 0))
                print(f"   - {path or name}   [{size}]")

    print("—"*70)
    print("Klaar.\n")
    conn.close()


if __name__ == "__main__":
    import sys
    main(show_details=("--details" in sys.argv))
