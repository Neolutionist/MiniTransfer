#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
debug_expirations.py
--------------------
Toon vervaldatums van pakketten, bijbehorende bestanden en publieke links.

  python3 debug_expirations.py
  python3 debug_expirations.py --details          # files per pakket + downloadlinks
  python3 debug_expirations.py --tenant oldehanter
  python3 debug_expirations.py --db /var/data/files_multi.db

Pad-resolutie:
  1) --db argument
  2) DATA_DIR env var → <DATA_DIR>/files_multi.db
  3) Fallback: <scriptdir>/data/files_multi.db

Hosts (voor de gegenereerde URLs):
  - Per tenant via TENANT_HOSTS env, formaat:
        "tenant1=host1.example.com, tenant2=host2.example.com"
  - Fallback: CANONICAL_HOST env (geen hardcoded default).
  - Anders: '<host onbekend>' in de URL — dan weet je dat de env mist.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


# -------------------- Pad-resolutie --------------------

def resolve_db_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    data_dir = os.environ.get("DATA_DIR")
    if data_dir:
        return (Path(data_dir) / "files_multi.db").resolve()
    return (Path(__file__).resolve().parent / "data" / "files_multi.db").resolve()


# -------------------- Hosts --------------------

def parse_tenant_hosts(envval: str) -> dict[str, str]:
    """Verwacht: 'tenant1=host1.example, tenant2=host2.example'"""
    out: dict[str, str] = {}
    for part in (envval or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        v = v.strip().lower()
        if k and v:
            out[k] = v
    return out


def host_for_tenant(tenant_id: str, tenant_hosts: dict[str, str], canonical: str) -> str:
    return tenant_hosts.get((tenant_id or "").lower(), canonical) or "<host onbekend>"


# -------------------- Helpers --------------------

def human_bytes(n: int) -> str:
    x = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if x < 1024.0 or u == "TB":
            return f"{int(x)} {u}" if u == "B" else f"{x:.1f} {u}"
        x /= 1024.0
    return f"{n} B"


def parse_iso(dt: str) -> datetime:
    """expires_at in de DB is ISO met tz-offset → fromisoformat parst direct."""
    return datetime.fromisoformat(dt)


def format_remaining(expires_at: str, now: datetime) -> str:
    try:
        dt_exp = parse_iso(expires_at)
    except (ValueError, TypeError):
        return "(ongeldige datum)"
    delta = dt_exp - now
    if delta.total_seconds() <= 0:
        return "EXPIRED"
    days = delta.days
    hrs = (delta.seconds // 3600) % 24
    return f"{days}d {hrs}h"


# -------------------- DB queries --------------------

def load_packages(conn: sqlite3.Connection, only_tenant: str | None) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    if only_tenant:
        rows = conn.execute(
            """
            SELECT token, title, tenant_id, created_at, expires_at
            FROM packages
            WHERE tenant_id = ?
            ORDER BY expires_at ASC
            """,
            (only_tenant,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT token, title, tenant_id, created_at, expires_at
            FROM packages
            ORDER BY expires_at ASC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def package_stats(conn: sqlite3.Connection, token: str, tenant_id: str) -> tuple[int, int]:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cnt, COALESCE(SUM(size_bytes), 0) AS total
        FROM items
        WHERE token = ? AND tenant_id = ?
        """,
        (token, tenant_id),
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


def list_items(conn: sqlite3.Connection, token: str, tenant_id: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, name, path, size_bytes
        FROM items
        WHERE token = ? AND tenant_id = ?
        ORDER BY path, name
        """,
        (token, tenant_id),
    ).fetchall()
    return [dict(r) for r in rows]


# -------------------- URL builders --------------------

def build_package_url(host: str, token: str) -> str:
    return f"https://{host}/p/{quote(token or '', safe='')}"


def build_file_url(host: str, token: str, item_id: int) -> str:
    return f"https://{host}/file/{quote(token or '', safe='')}/{int(item_id)}"


# -------------------- Main --------------------

def main(args: argparse.Namespace) -> None:
    db_path = resolve_db_path(args.db)
    if not db_path.exists():
        print(f"🚫 Database niet gevonden op: {db_path}", file=sys.stderr)
        sys.exit(3)

    canonical = os.environ.get("CANONICAL_HOST", "").strip().lower()
    tenant_hosts = parse_tenant_hosts(os.environ.get("TENANT_HOSTS", ""))

    conn = sqlite3.connect(db_path)
    try:
        pkgs = load_packages(conn, args.tenant)
        if not pkgs:
            scope = f" voor tenant '{args.tenant}'" if args.tenant else ""
            print(f"ℹ️  Geen pakketten gevonden{scope}.")
            return

        now = datetime.now(timezone.utc)
        print(f"\n📦 Overzicht vervaldata per pakket  ({db_path})\n")

        sep = "—" * 86
        for p in pkgs:
            token = p["token"]
            title = p.get("title") or ""
            tenant_id = p.get("tenant_id") or ""
            created_at = p.get("created_at") or ""
            expires_at = p.get("expires_at") or ""

            rem = format_remaining(expires_at, now)
            cnt, total = package_stats(conn, token, tenant_id)
            host = host_for_tenant(tenant_id, tenant_hosts, canonical)
            link = build_package_url(host, token)

            print(sep)
            print(f"Tenant       : {tenant_id}")
            print(f"Token        : {token}")
            print(f"Title        : {title}")
            print(f"Aangemaakt   : {created_at}")
            print(f"Vervalt op   : {expires_at}   (remaining: {rem})")
            print(f"Files        : {cnt}  (totaal {human_bytes(total)})")
            print(f"Link         : {link}")

            if args.details and cnt:
                for it in list_items(conn, token, tenant_id):
                    item_id = it["id"]
                    name = it["name"]
                    path = it["path"]
                    size = human_bytes(int(it["size_bytes"] or 0))
                    file_url = build_file_url(host, token, item_id)
                    print(f"   - {path or name}   [{size}]")
                    print(f"       ↳ {file_url}")

        print(sep)
        print("Klaar.\n")
    finally:
        conn.close()


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Toon pakketvervaldata en links.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--details", action="store_true",
                    help="Toon per bestand details + downloadlink.")
    ap.add_argument("--tenant", default=None,
                    help="Filter op één tenant (bv. 'oldehanter').")
    ap.add_argument("--db", default=None,
                    help="Override pad naar files_multi.db.")
    return ap.parse_args()


if __name__ == "__main__":
    main(parse_args())
