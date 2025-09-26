#!/usr/bin/env python3
import os, sqlite3
from pathlib import Path
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError, BotoCoreError

# ---- S3 config uit env ----
S3_BUCKET       = os.environ["S3_BUCKET"]
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]
S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")

s3 = boto3.client("s3", region_name=S3_REGION, endpoint_url=S3_ENDPOINT_URL)

# ---- DB pad: gelijk aan je Flask-app ----
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)            # voor journal/wal
DB_PATH  = DATA_DIR / "files_multi.db"

def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def cleanup_expired(dry_run=False):
    if not DB_PATH.exists():
        raise RuntimeError(f"DB niet gevonden op {DB_PATH}. Zet DATA_DIR correct of gebruik hetzelfde pad als de app.")
    print(f"[i] Gebruik DB: {DB_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    now = utcnow_iso()
    pkgs = c.execute("""
        SELECT token, tenant_id FROM packages
        WHERE expires_at < ?
    """, (now,)).fetchall()

    total_deleted = 0
    for pkg in pkgs:
        token = pkg["token"]; tenant = pkg["tenant_id"]

        items = c.execute("""
            SELECT id, s3_key FROM items
            WHERE token=? AND tenant_id=?
        """, (token, tenant)).fetchall()

        for it in items:
            key = it["s3_key"]
            print(f"[DELETE] s3://{S3_BUCKET}/{key}")
            if not dry_run:
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=key)
                except (ClientError, BotoCoreError) as e:
                    print(f"  -> S3 delete failed: {e}")

        if not dry_run:
            c.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, tenant))
            c.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, tenant))
            conn.commit()

        total_deleted += len(items)

    conn.close()
    print(f"Klaar. Verwijderde objecten: {total_deleted} (dry_run={dry_run})")

if __name__ == "__main__":
    cleanup_expired(dry_run=False)
