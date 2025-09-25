import os
import sqlite3
from datetime import datetime, timezone
import boto3
import traceback

# Constants
DB_PATH = "/var/data/files_multi.db"
B2_BUCKET_NAME = "MiniTransfer"
B2_ENDPOINT_URL = "https://s3.eu-central-003.backblazeb2.com"

def cleanup():
    # Check if the database exists
    print(f"🔍 Checking DB path: {DB_PATH}")
    if not os.path.exists(DB_PATH):
        raise FileNotFoundError(f"❌ Database file not found at {DB_PATH}")

    # Log environment (for debugging cron context)
    print("🔐 AWS_ACCESS_KEY_ID:", os.getenv("AWS_ACCESS_KEY_ID"))
    print("🔐 AWS_SECRET_ACCESS_KEY:", ("*" * 4) + os.getenv("AWS_SECRET_ACCESS_KEY")[-4:] if os.getenv("AWS_SECRET_ACCESS_KEY") else None)

    # Create S3 client
    s3 = boto3.client(
        "s3",
        endpoint_url=B2_ENDPOINT_URL,
    )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    now = datetime.now(timezone.utc)
    print(f"🕒 Current UTC time: {now.isoformat()}")

    # Fetch tokens with expiration
    c.execute("SELECT token, expires_at FROM packages WHERE expires_at IS NOT NULL")
    rows = c.fetchall()
    print(f"📦 Found {len(rows)} expirable packages")

    for token, expires_at in rows:
        try:
            expires_at_dt = datetime.fromisoformat(expires_at)
        except Exception as e:
            print(f"⚠️ Skipping token '{token}' - invalid expires_at: {expires_at}")
            continue

        if expires_at_dt < now:
            prefix = f"uploads/{token}/"
            print(f"🧹 Cleaning up expired token: {token}")

            try:
                resp = s3.list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix=prefix)
                if "Contents" in resp:
                    for obj in resp["Contents"]:
                        key = obj["Key"]
                        print(f" - 🗑 Deleting: {key}")
                        s3.delete_object(Bucket=B2_BUCKET_NAME, Key=key)
                else:
                    print(" - (No objects found to delete)")

                # Clean up DB
                c.execute("DELETE FROM items WHERE token=?", (token,))
                c.execute("DELETE FROM packages WHERE token=?", (token,))
                conn.commit()
                print("✅ Deleted from database")

            except Exception as s3_error:
                print(f"❌ S3 Error for token {token}: {s3_error}")

    conn.close()
    print("✅ Cleanup completed")

if __name__ == "__main__":
    try:
        cleanup()
    except Exception as e:
        print("❌ Unhandled exception occurred:")
        traceback.print_exc()
        exit(1)
