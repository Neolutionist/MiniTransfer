import os
import sqlite3
from datetime import datetime, timezone
import boto3

# Backblaze S3 client (pakt AWS_* variabelen automatisch op)
s3 = boto3.client(
    "s3",
    endpoint_url="https://s3.eu-central-003.backblazeb2.com",
)

# Zet hier je bucketnaam (kan ook via ENV)
B2_BUCKET_NAME = "MiniTransfer"

DB_PATH = "/var/data/files_multi.db"

def cleanup():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    now = datetime.now(timezone.utc).isoformat()

    c.execute("SELECT token, expires_at FROM packages WHERE expires_at IS NOT NULL")
    rows = c.fetchall()

    for token, expires_at in rows:
        if expires_at < now:
            prefix = f"uploads/{token}/"
            print(f"Verwijderen: {prefix}")

            resp = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
            if "Contents" in resp:
                for obj in resp["Contents"]:
                    key = obj["Key"]
                    print(f" - deleting {key}")
                    s3.delete_object(Bucket=B2_BUCKET_NAME, Key=key)

            # opschonen in DB
            c.execute("DELETE FROM items WHERE token=?", (token,))
            c.execute("DELETE FROM packages WHERE token=?", (token,))
            conn.commit()

    conn.close()

if __name__ == "__main__":
    cleanup()
