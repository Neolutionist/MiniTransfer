#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
insert_test_file.py
-------------------
Voeg een testpakket toe met (standaard) een vervaldatum in het verleden,
om de cleanup-flow te kunnen testen.

  # Standaard: 1 dag verlopen
  python3 insert_test_file.py

  # Verloopt over 5 minuten (positieve waarde = toekomst)
  python3 insert_test_file.py --expires-in-days 0.0035

  # Andere tenant + grootte + titel
  python3 insert_test_file.py --tenant oldehanter --size-bytes 12345 --title "Mijn test"

  # Specifieke DB
  python3 insert_test_file.py --db /var/data/files_multi.db

Pad-resolutie:
  1) --db argument
  2) DATA_DIR env var → <DATA_DIR>/files_multi.db
  3) Fallback: <scriptdir>/data/files_multi.db
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


def resolve_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return (Path(data_dir) / "files_multi.db").resolve()
    return (Path(__file__).resolve().parent / "data" / "files_multi.db").resolve()


def required_tables_present(conn: sqlite3.Connection) -> list[str]:
    """Returns lijst met ontbrekende tabellen ('packages', 'items')."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('packages', 'items')"
    ).fetchall()
    present = {r[0] for r in rows}
    return [t for t in ("packages", "items") if t not in present]


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Voeg een (verlopen) testpakket toe.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--db", default=None, help="Override pad naar files_multi.db.")
    ap.add_argument("--tenant", default="test_tenant", help="tenant_id (default: test_tenant).")
    ap.add_argument("--title", default="ExpiredTest", help="Titel van het pakket.")
    ap.add_argument("--size-bytes", type=int, default=5678,
                    help="Grootte van het dummy-bestand in bytes (default: 5678).")
    ap.add_argument("--expires-in-days", type=float, default=-1.0,
                    help="Vervaldatum in dagen vanaf nu. Negatief = in het verleden. "
                         "Default: -1 (1 dag geleden verlopen).")
    return ap.parse_args()


def main() -> None:
    args = parse_args()
    db_path = resolve_db_path(args.db)

    if not db_path.exists():
        print(f"🚫 Database niet gevonden op: {db_path}", file=sys.stderr)
        sys.exit(3)

    conn = sqlite3.connect(db_path)
    try:
        missing = required_tables_present(conn)
        if missing:
            print(f"🚫 Vereiste tabel(len) ontbreken: {missing}. "
                  f"Is dit wel de juiste DB? ({db_path})", file=sys.stderr)
            sys.exit(4)

        token = str(uuid.uuid4())[:8]
        tenant_id = args.tenant
        title = args.title
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        expires_at = (now + timedelta(days=args.expires_in_days)).isoformat()

        conn.execute(
            """
            INSERT INTO packages (token, expires_at, password_hash, created_at, title, tenant_id)
            VALUES (?, ?, NULL, ?, ?, ?)
            """,
            (token, expires_at, created_at, title, tenant_id),
        )
        conn.execute(
            """
            INSERT INTO items (token, s3_key, name, path, size_bytes, tenant_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                f"{tenant_id}/{token}/expired_dummy.txt",
                "expired_dummy.txt",
                "",
                int(args.size_bytes),
                tenant_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()

    status = "verlopen" if args.expires_in_days < 0 else "actief"
    print(f"✅ Testpakket toegevoegd ({status})")
    print(f"DB         : {db_path}")
    print(f"Token      : {token}")
    print(f"Tenant ID  : {tenant_id}")
    print(f"Vervalt op : {expires_at}")


if __name__ == "__main__":
    main()
