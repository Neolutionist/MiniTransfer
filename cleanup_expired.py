#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cleanup_expired.py
------------------
Verwijdert verlopen pakketten uit de SQLite-DB én bijbehorende S3-objecten.

Robuuste punten:
- Valt automatisch terug op ./data als DATA_DIR onbruikbaar is.
- Werkt ook als cron in een andere working directory draait.
- CLI-argumenten: --dry-run, --tenant, --db, --verbose.
- Houdt rekening met tenant_id-kolommen (indien aanwezig).
"""

from __future__ import annotations
import os
import sys
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError, BotoCoreError

# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Opschonen van verlopen pakketten en S3-objecten.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Toon wat er zou worden verwijderd, maar voer niets echt uit.")
    ap.add_argument("--tenant", default=None,
                    help="Alleen deze tenant opruimen (bijv. 'downloadlink'). Laat leeg voor alle tenants.")
    ap.add_argument("--db", default=None,
                    help="Volledig pad naar files_multi.db (override).")
    ap.add_argument("--verbose", "-v", action="store_true", help="Extra logging.")
    return ap.parse_args()

# ---------- Tijd ----------

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Pad & DB ----------

def resolve_data_dir(verbose: bool = False) -> Path:
    """
    Bepaal een veilige DATA_DIR:
    1) Als DATA_DIR env bestaat, probeer die; zo niet schrijfbaar -> fallback.
    2) Fallback = <scriptdir>/data
    """
    script_dir = Path(__file__).resolve().parent
    cfg = os.environ.get("DATA_DIR")
    candidate = Path(cfg) if cfg else (script_dir / "data")

    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if verbose:
            print(f"[i] DATA_DIR = {candidate}")
        return candidate
    except PermissionError:
        # Fallback naar project/data
        fallback = script_dir / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        if verbose:
            print(f"[!] Geen rechten op {candidate}, fallback naar {fallback}")
        return fallback

def open_db(db_path: Path, verbose: bool = False) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"Database niet gevonden: {db_path}")
    if verbose:
        print(f"[i] DB_PATH = {db_path}")
    conn = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # iets vriendelijker bij locks
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
    except Exception:
        pass
    return conn

def table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

# ---------- S3 ----------

def make_s3_client():
    # Leest credentials uit env (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY), net als de app
    S3_BUCKET       = os.environ["S3_BUCKET"]
    S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]
    S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")
    s3 = boto3.client("s3", region_name=S3_REGION, endpoint_url=S3_ENDPOINT_URL)
    return s3, S3_BUCKET

# ---------- Kern ----------

def cleanup_expired(db_path: Path, dry_run: bool = False, only_tenant: str | None = None, verbose: bool = False) -> int:
    """
    Returnt het aantal verwijderde S3-objecten (of ‘te verwijderen’ bij dry-run).
    """
    s3, bucket = make_s3_client()
    conn = open_db(db_path, verbose=verbose)
    cur = conn.cursor()

    now_iso = utcnow_iso()

    # Bepaal of tenant_id-kolom bestaat
    has_tenant_pkgs = table_has_column(conn, "packages", "tenant_id")
    has_tenant_items = table_has_column(conn, "items", "tenant_id")

    # Stel query samen
    where = "expires_at < ?"
    params: list = [now_iso]
    if has_tenant_pkgs and only_tenant:
        where += " AND tenant_id = ?"
        params.append(only_tenant)

    pkgs = cur.execute(f"SELECT token{', tenant_id' if has_tenant_pkgs else ''} FROM packages WHERE {where}", params).fetchall()

    if verbose:
        total = len(pkgs)
        scope = f" (tenant='{only_tenant}')" if only_tenant else ""
        print(f"[i] Verlopen pakketten gevonden: {total}{scope}")

    total_deleted_objects = 0

    for row in pkgs:
        token = row["token"]
        tenant = row["tenant_id"] if has_tenant_pkgs else None

        # items ophalen
        if has_tenant_items:
            it_rows = cur.execute(
                "SELECT id, s3_key FROM items WHERE token=? AND tenant_id=?",
                (token, tenant)
            ).fetchall()
        else:
            it_rows = cur.execute(
                "SELECT id, s3_key FROM items WHERE token=?",
                (token,)
            ).fetchall()

        # S3 delete per item
        for it in it_rows:
            key = it["s3_key"]
            print(f"[DEL] s3://{bucket}/{key}")
            total_deleted_objects += 1
            if not dry_run:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except (ClientError, BotoCoreError) as e:
                    print(f"  -> S3 delete failed: {e}")

        # DB opschonen
        if not dry_run:
            if has_tenant_items:
                cur.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, tenant))
            else:
                cur.execute("DELETE FROM items WHERE token=?", (token,))

            if has_tenant_pkgs:
                cur.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, tenant))
            else:
                cur.execute("DELETE FROM packages WHERE token=?", (token,))
            conn.commit()

        if verbose:
            print(f"[i] Pakket {token} {'(tenant '+tenant+')' if tenant else ''} opgeschoond "
                  f"— items: {len(it_rows)}")

    conn.close()
    print(f"Klaar. Verwijderde objecten: {total_deleted_objects} (dry_run={dry_run})")
    return total_deleted_objects

# ---------- main ----------

def main():
    args = parse_args()

    # DB pad bepalen
    if args.db:
        db_path = Path(args.db).expanduser().resolve()
    else:
        data_dir = resolve_data_dir(verbose=args.verbose)
        db_path = (data_dir / "files_multi.db").resolve()

    try:
        cleanup_expired(
            db_path=db_path,
            dry_run=args.dry_run,
            only_tenant=args.tenant,
            verbose=args.verbose,
        )
    except KeyError as e:
        # Ontbrekende S3 env var
        print(f"[FOUT] Ontbrekende environment variabele: {e}. "
              f"Zorg voor S3_BUCKET / S3_ENDPOINT_URL (en AWS_ACCESS_KEY_ID/SECRET).", file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError as e:
        print(f"[FOUT] {e}", file=sys.stderr)
        sys.exit(3)
    except sqlite3.OperationalError as e:
        print(f"[FOUT] SQLite-fout: {e} — klopt het DB-pad? ({db_path})", file=sys.stderr)
        sys.exit(4)
    except PermissionError as e:
        print(f"[FOUT] Rechtenprobleem: {e}. Hint: zet DATA_DIR=/opt/render/project/src/data of kies --db.", file=sys.stderr)
        sys.exit(5)
    except Exception as e:
        print(f"[FOUT] Onverwachte fout: {e}", file=sys.stderr)
        sys.exit(1)

if __name__ == "__main__":
    main()
