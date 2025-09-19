#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask, request, redirect, url_for, abort, render_template_string, session, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

# ---------------- Config ----------------
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "files.db"

AUTH_EMAIL = "info@oldehanter.nl"
AUTH_PASSWORD = "Hulsmaat"

MAX_RELAY_MB = int(os.environ.get("MAX_RELAY_MB", "200"))
MAX_RELAY_BYTES = MAX_RELAY_MB * 1024 * 1024

S3_BUCKET       = os.environ["S3_BUCKET"]
S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]

# Path-style addressing (werkt ook als je bucket hoofdletters heeft)
s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
    config=BotoConfig(s3={"addressing_style": "path"})
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")

# --------------- DB --------------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = db()
    c.execute("""
      CREATE TABLE IF NOT EXISTS files (
        token TEXT PRIMARY KEY,
        stored_path TEXT NOT NULL,
        original_name TEXT NOT NULL,
        password_hash TEXT,
        expires_at TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        created_at TEXT NOT NULL
      )
    """)
    c.commit(); c.close()

init_db()

# -------------- TEMPLATES --------------
INDEX_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Olde Hanter – Upload</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:linear-gradient(135deg,#cfe5ff,#e7dcff);margin:0}
  .wrap{max-width:980px;margin:6vh auto;padding:0 16px}
  .top{display:flex;justify-content:space-between;align-items:center}
  h1{color:#003366;margin:.5rem 0}
  .card{background:rgba(255,255,255,.85);backdrop-filter:blur(8px);border:1px solid rgba(255,255,255,.5);border-radius:18px;padding:18px;box-shadow:0 16px 36px rgba(0,0,0,.12)}
  label{display:block;margin:.6rem 0 .25rem;font-weight:600}
  input[type=file],input[type=number],input[type=password]{width:100%;padding:.9rem 1rem;border:1px solid #d1d5db;border-radius:12px;background:#fff}
  button{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:12px;background:#003366;color:#fff;font-weight:700;cursor:pointer}
  .note{color:#334155}
  .footer{color:#475569;text-align:center;margin-top:1rem}
</style></head><body>
<div class="wrap">
  <div class="top">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div>Ingelogd als {{ user }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  <form id="f" class="card" enctype="multipart/form-data" autocomplete="off">
    <label>Bestand</label>
    <input type="file" name="file" id="file" required>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label>Verloopt over (dagen)</label>
        <input type="number" name="expiry_days" id="exp" min="1" value="24">
      </div>
      <div>
        <label>Wachtwoord (optioneel)</label>
        <input type="password" name="password" id="pw" placeholder="Laat leeg voor geen wachtwoord" autocomplete="new-password">
      </div>
    </div>

    <button type="submit">Uploaden</button>
    <p class="note">Max {{ max_mb }} MB via server-relay.</p>
  </form>

  <div id="result"></div>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  const form = document.getElementById('f');
  const file = document.getElementById('file');
  const resBox = document.getElementById('result');

  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    if(!file.files[0]){ return alert("Kies een bestand"); }
    const fd = new FormData(form);

    let res;
    try{
      res = await fetch("{{ url_for('upload_relay') }}", { method:"POST", body: fd });
    }catch(e){
      return alert("Verbinding met server mislukt.");
    }
    const data = await res.json().catch(()=>({ok:false,error:"Onbekende fout"}));
    if(!res.ok || !data.ok){ return alert(data.error || ("Fout: "+res.status)); }

    resBox.innerHTML = `
      <div class="card" style="margin-top:1rem">
        <strong>Deelbare link</strong><br>
        <input style="width:100%;padding:.7rem;border-radius:10px;border:1px solid #d1d5db" value="${data.link}" readonly>
        <button style="margin-top:.6rem" onclick="navigator.clipboard.writeText('${data.link}').then(()=>alert('Link gekopieerd'))">Kopieer</button>
      </div>`;
  });
</script>
</body></html>
"""

LOGIN_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef2f7;margin:0}
  .wrap{max-width:480px;margin:10vh auto;padding:0 16px}
  .card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px;box-shadow:0 8px 24px rgba(0,0,0,.08)}
  label{display:block;margin:.5rem 0 .25rem;font-weight:600}
  input{width:100%;padding:.9rem 1rem;border:1px solid #d1d5db;border-radius:12px}
  button{margin-top:1rem;padding:.9rem 1.1rem;border:0;border-radius:10px;background:#003366;color:#fff}
</style></head><body>
<div class="wrap"><div class="card">
  <h2>Inloggen</h2>
  {% if error %}<div style="color:#b91c1c">{{ error }}</div>{% endif %}
  <form method="post" autocomplete="on">
    <label>E-mail</label>
    <input name="email" type="email" value="info@oldehanter.nl" autocomplete="username" required>
    <label>Wachtwoord</label>
    <input name="password" type="password" required>
    <button>Inloggen</button>
  </form>
</div></div></body></html>
"""

DOWNLOAD_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Download</title>
<style>
 body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f5f7fb;margin:0}
 .wrap{max-width:760px;margin:6vh auto;padding:0 16px}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:16px;padding:18px;box-shadow:0 8px 24px rgba(0,0,0,.08)}
 .btn{display:inline-block;padding:.9rem 1.1rem;border-radius:10px;background:#003366;color:#fff;text-decoration:none}
</style></head><body>
<div class="wrap"><div class="card">
  <h2>Download bestand</h2>
  <p><strong>Bestandsnaam:</strong> {{ name }}</p>
  <p><strong>Grootte:</strong> {{ size }}</p>
  <p><strong>Verloopt:</strong> {{ exp }}</p>
  <a class="btn" href="{{ url_for('download_file', token=token) }}">Download</a>
</div></div></body></html>
"""

# -------------- Helpers --------------
def logged_in(): return session.get("authed", False)

def human(n:int):
    x = float(n)
    for u in ["B","KB","MB","GB","TB"]:
        if x < 1024 or u == "TB":
            return f"{x:.1f} {u}" if u!="B" else f"{int(x)} {u}"
        x/=1024

# -------------- Routes --------------
@app.route("/")
def index():
    if not logged_in(): return redirect(url_for("login"))
    return render_template_string(INDEX_HTML, user=session.get("user"), max_mb=MAX_RELAY_MB)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if (request.form.get("email") or "").lower()==AUTH_EMAIL and request.form.get("password")==AUTH_PASSWORD:
            session["authed"] = True
            session["user"] = AUTH_EMAIL
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="Onjuiste inloggegevens.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# Browser → server → B2
@app.route("/upload-relay", methods=["POST"])
def upload_relay():
    if not logged_in(): return abort(401)

    f = request.files.get("file")
    if not f or f.filename == "":
        return jsonify(ok=False, error="Geen bestand"), 400

    # rudimentaire grootte-check
    if request.content_length and request.content_length > MAX_RELAY_BYTES:
        return jsonify(ok=False, error=f"Bestand groter dan {MAX_RELAY_MB} MB"), 413

    filename = secure_filename(f.filename)
    token = uuid.uuid4().hex[:10]
    object_key = f"uploads/{token}__{filename}"

    expiry_days = request.form.get("expiry_days", "24")
    password = request.form.get("password") or ""
    pw_hash = generate_password_hash(password) if password else None

    try:
        # stream direct naar B2
        s3.upload_fileobj(f.stream, S3_BUCKET, object_key)
        head = s3.head_object(Bucket=S3_BUCKET, Key=object_key)
        size_bytes = int(head["ContentLength"])
    except Exception as e:
        app.logger.error(f"upload_relay error: {e}")
        return jsonify(ok=False, error="Upload naar opslag mislukt"), 500

    expires_at = (datetime.now(timezone.utc) + timedelta(days=float(expiry_days or 24))).isoformat()

    c = db()
    c.execute("INSERT INTO files(token,stored_path,original_name,password_hash,expires_at,size_bytes,created_at) VALUES(?,?,?,?,?,?,?)",
              (token, object_key, filename, pw_hash, expires_at, size_bytes, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()

    link = url_for("download", token=token, _external=True)
    return jsonify(ok=True, link=link)

@app.route("/d/<token>", methods=["GET","POST"])
def download(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row: abort(404)

    # verlopen?
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        try: s3.delete_object(Bucket=S3_BUCKET, Key=row["stored_path"])
        except: pass
        c = db(); c.execute("DELETE FROM files WHERE token=?", (token,)); c.commit(); c.close()
        abort(410)

    # wachtwoord
    if row["password_hash"]:
        if request.method == "GET":
            return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                        <h3>Voer wachtwoord in</h3>
                        <input type="password" name="password" style="width:100%;padding:.7rem;border:1px solid #ccc;border-radius:8px" required>
                        <button style="margin-top:.6rem;padding:.6rem 1rem;border:0;border-radius:8px;background:#003366;color:#fff">Ontgrendel</button>
                      </form>"""
        if not check_password_hash(row["password_hash"], request.form.get("password","")):
            return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                        <h3 style="color:#b91c1c">Onjuist wachtwoord</h3>
                        <input type="password" name="password" style="width:100%;padding:.7rem;border:1px solid #ccc;border-radius:8px" required>
                        <button style="margin-top:.6rem;padding:.6rem 1rem;border:0;border-radius:8px;background:#003366;color:#fff">Opnieuw</button>
                      </form>"""

    exp = datetime.fromisoformat(row["expires_at"]).replace(second=0, microsecond=0).strftime("%d-%m-%Y %H:%M")
    return render_template_string(
        DOWNLOAD_HTML,
        name=row["original_name"], size=human(int(row["size_bytes"])), exp=exp, token=token
    )

@app.route("/dl/<token>")
def download_file(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row: abort(404)
    url = s3.generate_presigned_url("get_object", Params={"Bucket": S3_BUCKET, "Key": row["stored_path"]}, ExpiresIn=3600)
    return redirect(url)

# eenvoudige check
@app.route("/health-s3")
def health():
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        return {"ok": True, "bucket": S3_BUCKET}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

if __name__ == "__main__":
    app.run(debug=True)
