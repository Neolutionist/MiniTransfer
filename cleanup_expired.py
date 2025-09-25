#!/usr/bin/env python3
import os, sqlite3
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError, BotoCoreError

# ---- S3 config uit je bestaande env ----
S3_BUCKET       = os.environ["S3_BUCKET"]
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]
S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")

s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
)

# Jouw DB op de Render-disk
DB_PATH = os.environ.get("DATA_DIR", "/var/data") + "/files_multi.db"

def utcnow_iso():
    return datetime.now(timezone.utc).isoformat()

def cleanup_expired(dry_run=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # Zoek verlopen pakketten
    now = utcnow_iso()
    pkgs = c.execute("""
        SELECT token, tenant_id FROM packages
        WHERE expires_at < ?
    """, (now,)).fetchall()

    total_deleted = 0
    for pkg in pkgs:
        token = pkg["token"]
        tenant = pkg["tenant_id"]

        # Alle items (S3 objecten) bij dit pakket
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
            # DB opruimen
            c.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, tenant))
            c.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, tenant))
            conn.commit()

        total_deleted += len(items)

    conn.close()
    print(f"Klaar. Verwijderde objecten: {total_deleted} (dry_run={dry_run})")

if __name__ == "__main__":
    # Zet dry_run=True om eerst te testen zonder echte deletes
    cleanup_expired(dry_run=False)
