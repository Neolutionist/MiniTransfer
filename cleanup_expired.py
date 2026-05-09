#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
cleanup_expired.py
------------------
Twee modi:

  1) LOCAL  – opent de SQLite-DB direct en ruimt verlopen pakketten + S3-objecten op.
              Voor handmatig/lokaal gebruik op de server zelf.

  2) REMOTE – roept POST /internal/cleanup aan op één of meerdere webservers.
              Bedoeld voor één centrale worker die meerdere servers schoonmaakt
              zonder zelf bij hun database of S3-credentials te kunnen.

De modus wordt automatisch gekozen:
  - Als --remote of CLEANUP_TARGETS gezet is → REMOTE.
  - Anders                                   → LOCAL.

Voorbeelden:
  # Lokaal (zoals voorheen):
  python3 cleanup_expired.py --verbose
  python3 cleanup_expired.py --dry-run --tenant oldehanter
  python3 cleanup_expired.py --db /var/data/files_multi.db

  # Remote (worker die beide servers afhandelt):
  export CLEANUP_TARGETS="https://server-a.example.com,https://server-b.example.com"
  export TASK_TOKEN="..."
  python3 cleanup_expired.py
  # of expliciet:
  python3 cleanup_expired.py --remote https://server-a.example.com,https://server-b.example.com \
                             --task-token "$TASK_TOKEN" --verbose

Environment variabelen:
  DATA_DIR          Map met files_multi.db (local mode). Default: /var/data, met
                    fallback naar <scriptdir>/data als /var/data niet schrijfbaar is.
  S3_BUCKET         Vereist in local mode.
  S3_ENDPOINT_URL   Vereist in local mode.
  S3_REGION         Optioneel (default eu-central-003).
  AWS_ACCESS_KEY_ID,
  AWS_SECRET_ACCESS_KEY  Standaard boto3-credentials.

  CLEANUP_TARGETS   Komma-gescheiden lijst URLs voor remote mode.
  TASK_TOKEN        Token voor X-Task-Token header in remote mode.
  CLEANUP_TIMEOUT   Timeout (seconden) per remote call. Default: 120.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# boto3 wordt alleen in local mode gebruikt; lazy importeren zodat een worker
# zonder S3-credentials/boto3 toch in remote mode kan draaien.

# ---------- CLI ----------

def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Opschonen van verlopen pakketten (local) of triggeren via /internal/cleanup (remote)."
    )

    # Gemeenschappelijk
    ap.add_argument("--dry-run", action="store_true",
                    help="Toon wat er zou worden verwijderd, maar voer niets echt uit.")
    ap.add_argument("--tenant", default=None,
                    help="Alleen deze tenant opruimen (bijv. 'oldehanter'). Laat leeg voor alle tenants.")
    ap.add_argument("--verbose", "-v", action="store_true", help="Extra logging.")

    # Local mode
    ap.add_argument("--db", default=None,
                    help="[LOCAL] Volledig pad naar files_multi.db (override).")

    # Remote mode
    ap.add_argument("--remote", default=None,
                    help="[REMOTE] Komma-gescheiden lijst van basis-URLs (bv. https://a.example,https://b.example). "
                         "Override van CLEANUP_TARGETS env var.")
    ap.add_argument("--task-token", default=None,
                    help="[REMOTE] X-Task-Token header. Override van TASK_TOKEN env var.")
    ap.add_argument("--timeout", type=int, default=None,
                    help="[REMOTE] Timeout per call in seconden. Default: CLEANUP_TIMEOUT env of 120.")

    return ap.parse_args()

# ---------- Tijd ----------

def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ---------- Pad & DB (local mode) ----------

def resolve_data_dir(verbose: bool = False) -> Path:
    """
    Bepaal een veilige DATA_DIR:
      1) Als DATA_DIR env bestaat, probeer die.
      2) Niet schrijfbaar / geen rechten → fallback naar <scriptdir>/data.
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
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.Error:
        pass
    return conn


def table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

# ---------- S3 (local mode) ----------

def make_s3_client():
    """Lazy: alleen importeren wanneer we daadwerkelijk lokaal opruimen."""
    try:
        import boto3
    except ImportError as e:
        raise RuntimeError(
            "boto3 is niet geïnstalleerd. Voor remote-mode is dit niet nodig; "
            "voor local-mode wel. (`pip install boto3`)"
        ) from e

    bucket = os.environ["S3_BUCKET"]
    endpoint = os.environ["S3_ENDPOINT_URL"]
    region = os.environ.get("S3_REGION", "eu-central-003")
    s3 = boto3.client("s3", region_name=region, endpoint_url=endpoint)
    return s3, bucket

# ---------- Local cleanup ----------

def cleanup_expired(
    db_path: Path,
    dry_run: bool = False,
    only_tenant: str | None = None,
    verbose: bool = False,
) -> int:
    """
    LOCAL mode: opent DB direct, verwijdert verlopen pakketten en hun S3-objecten.
    Returnt het aantal verwijderde (of bij dry-run 'te verwijderen') S3-objecten.
    """
    from botocore.exceptions import ClientError, BotoCoreError

    s3, bucket = make_s3_client()
    conn = open_db(db_path, verbose=verbose)
    cur = conn.cursor()

    now_iso = utcnow_iso()

    has_tenant_pkgs = table_has_column(conn, "packages", "tenant_id")
    has_tenant_items = table_has_column(conn, "items", "tenant_id")

    where = "expires_at < ?"
    params: list = [now_iso]
    if has_tenant_pkgs and only_tenant:
        where += " AND tenant_id = ?"
        params.append(only_tenant)

    select_cols = "token" + (", tenant_id" if has_tenant_pkgs else "")
    pkgs = cur.execute(
        f"SELECT {select_cols} FROM packages WHERE {where}",
        params,
    ).fetchall()

    if verbose:
        scope = f" (tenant='{only_tenant}')" if only_tenant else ""
        print(f"[i] Verlopen pakketten gevonden: {len(pkgs)}{scope}")

    total_deleted_objects = 0

    for row in pkgs:
        token = row["token"]
        tenant = row["tenant_id"] if has_tenant_pkgs else None

        if has_tenant_items and tenant is not None:
            it_rows = cur.execute(
                "SELECT id, s3_key FROM items WHERE token=? AND tenant_id=?",
                (token, tenant),
            ).fetchall()
        else:
            it_rows = cur.execute(
                "SELECT id, s3_key FROM items WHERE token=?",
                (token,),
            ).fetchall()

        for it in it_rows:
            key = it["s3_key"]
            print(f"[DEL] s3://{bucket}/{key}")
            total_deleted_objects += 1
            if not dry_run:
                try:
                    s3.delete_object(Bucket=bucket, Key=key)
                except (ClientError, BotoCoreError) as e:
                    print(f"  -> S3 delete failed: {e}")

        if not dry_run:
            if has_tenant_items and tenant is not None:
                cur.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, tenant))
            else:
                cur.execute("DELETE FROM items WHERE token=?", (token,))

            if has_tenant_pkgs and tenant is not None:
                cur.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, tenant))
            else:
                cur.execute("DELETE FROM packages WHERE token=?", (token,))
            conn.commit()

        if verbose:
            tenant_label = f" (tenant {tenant})" if tenant else ""
            print(f"[i] Pakket {token}{tenant_label} opgeschoond — items: {len(it_rows)}")

    conn.close()
    print(f"Klaar (local). Verwijderde objecten: {total_deleted_objects} (dry_run={dry_run})")
    return total_deleted_objects

# ---------- Remote cleanup ----------

def _parse_targets(raw: str) -> list[str]:
    """'https://a, https://b/' -> ['https://a', 'https://b']"""
    out = []
    for part in (raw or "").split(","):
        part = part.strip().rstrip("/")
        if part:
            out.append(part)
    return out


def cleanup_remote(
    targets: list[str],
    task_token: str,
    dry_run: bool = False,
    only_tenant: str | None = None,
    verbose: bool = False,
    timeout: int = 120,
) -> int:
    """
    REMOTE mode: roep POST /internal/cleanup aan op elke target.
    Returnt het totaal aantal gerapporteerde verwijderde objecten over alle targets.
    Een falende target stopt het proces niet — andere targets worden alsnog verwerkt.
    """
    if not task_token:
        raise RuntimeError(
            "TASK_TOKEN ontbreekt. Zet TASK_TOKEN env var of geef --task-token mee."
        )

    total = 0
    failures = 0

    qs = {}
    if dry_run:
        qs["dry"] = "1"
    if only_tenant:
        qs["tenant"] = only_tenant
    if verbose:
        qs["verbose"] = "1"
    query = ("?" + urllib.parse.urlencode(qs)) if qs else ""

    for base in targets:
        url = f"{base}/internal/cleanup{query}"
        if verbose:
            print(f"[i] POST {url}")

        req = urllib.request.Request(
            url,
            data=b"",
            method="POST",
            headers={
                "X-Task-Token": task_token,
                "User-Agent": "cleanup_expired/remote",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                status = resp.status
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            print(f"[FOUT] {base}: HTTP {e.code} {e.reason} — {err_body[:300]}", file=sys.stderr)
            failures += 1
            continue
        except urllib.error.URLError as e:
            print(f"[FOUT] {base}: kon endpoint niet bereiken — {e.reason}", file=sys.stderr)
            failures += 1
            continue
        except TimeoutError:
            print(f"[FOUT] {base}: timeout na {timeout}s", file=sys.stderr)
            failures += 1
            continue

        # Antwoord verwerken
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print(f"[!] {base}: HTTP {status}, niet-JSON antwoord: {body[:300]}")
            failures += 1
            continue

        if not data.get("ok"):
            print(f"[!] {base}: server meldt fout — {data}")
            failures += 1
            continue

        deleted = int(data.get("deleted", 0))
        total += deleted
        print(f"[OK] {base}: deleted={deleted} dry={data.get('dry')} tenant={data.get('tenant')}")

    print(f"Klaar (remote). Totaal verwijderde objecten: {total} "
          f"({len(targets) - failures}/{len(targets)} targets succesvol, dry_run={dry_run})")

    if failures and failures == len(targets):
        # Alle targets faalden → exitcode niet-nul
        raise RuntimeError("Alle remote targets zijn gefaald.")
    return total

# ---------- main ----------

def main():
    args = parse_args()

    # Bepaal modus
    targets_raw = args.remote if args.remote is not None else os.environ.get("CLEANUP_TARGETS", "")
    targets = _parse_targets(targets_raw)

    if targets:
        # REMOTE mode
        token = args.task_token or os.environ.get("TASK_TOKEN", "")
        timeout = args.timeout or int(os.environ.get("CLEANUP_TIMEOUT", "120"))
        try:
            cleanup_remote(
                targets=targets,
                task_token=token,
                dry_run=args.dry_run,
                only_tenant=args.tenant,
                verbose=args.verbose,
                timeout=timeout,
            )
        except RuntimeError as e:
            print(f"[FOUT] {e}", file=sys.stderr)
            sys.exit(6)
        return

    # LOCAL mode
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
        print(f"[FOUT] Ontbrekende environment variabele: {e}. "
              f"Zorg voor S3_BUCKET / S3_ENDPOINT_URL (en AWS_ACCESS_KEY_ID/SECRET).",
              file=sys.stderr)
        sys.exit(2)
    except FileNotFoundError as e:
        print(f"[FOUT] {e}", file=sys.stderr)
        sys.exit(3)
    except sqlite3.OperationalError as e:
        print(f"[FOUT] SQLite-fout: {e} — klopt het DB-pad? ({db_path})", file=sys.stderr)
        sys.exit(4)
    except PermissionError as e:
        print(f"[FOUT] Rechtenprobleem: {e}. "
              f"Hint: zet DATA_DIR=/opt/render/project/src/data of kies --db.",
              file=sys.stderr)
        sys.exit(5)
    except Exception as e:
        print(f"[FOUT] Onverwachte fout: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
