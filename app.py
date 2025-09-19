#!/usr/bin/env python3
"""
Olde Hanter - MiniTransfer (Flask)
- Upload (tot MAX_CONTENT_LENGTH) met optioneel wachtwoord
- Deelbare link /d/<token> (toont branded downloadpagina)
- Directe download via /dl/<token>
- Verlooptijd in DAGEN (default 3)
- Verlooptijd op downloadpagina afgerond op MINUTEN
- SQLite metadata, lokale bestandsopslag
- Simpele cleanup van verlopen bestanden op iedere request
"""

import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, send_file, abort, flash, render_template_string
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------------------- Config ----------------------
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "files.db"

MAX_CONTENT_LENGTH = 512 * 1024 * 1024  # 512 MB per bestand (pas aan naar wens)
ALLOWED_EXTENSIONS = None  # bv. {"pdf","zip","jpg"} om te beperken

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", os.urandom(16)),
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
)
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------- Database ----------------------
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    con = get_db()
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            token TEXT PRIMARY KEY,
            stored_path TEXT NOT NULL,
            original_name TEXT NOT NULL,
            password_hash TEXT,
            expires_at TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.commit()
    con.close()

# ---------------------- Cleanup ----------------------
@app.before_request
def cleanup_expired():
    now = datetime.now(timezone.utc)
    con = get_db()
    rows = con.execute("SELECT token, stored_path, expires_at FROM files").fetchall()
    to_delete = []
    for r in rows:
        try:
            if datetime.fromisoformat(r["expires_at"]) <= now:
                to_delete.append((r["token"], r["stored_path"]))
        except Exception:
            continue
    for token, spath in to_delete:
        try:
            Path(spath).unlink(missing_ok=True)
        except Exception:
            pass
        con.execute("DELETE FROM files WHERE token=?", (token,))
    if to_delete:
        con.commit()
    con.close()

# ---------------------- Helpers ----------------------
def allowed_file(filename: str) -> bool:
    if not ALLOWED_EXTENSIONS:
        return True
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

def days_to_timedelta(days_str: str) -> timedelta:
    try:
        d = float(days_str)
        if d <= 0:
            d = 3.0
    except Exception:
        d = 3.0
    return timedelta(days=d)

def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

# ---------------------- Templates ----------------------
INDEX_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Olde Hanter Transfer</title>
  <!-- Inline favicon (geen externe file nodig) -->
  <link rel="icon" type="image/svg+xml"
        href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 120'%3E%3Crect width='120' height='120' rx='16' fill='%23003366'/%3E%3Ctext x='50%25' y='54%25' dominant-baseline='middle' text-anchor='middle' font-size='44' font-family='Arial, Helvetica, sans-serif' fill='white'%3EOH%3C/text%3E%3C/svg%3E"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    :root{ --oh-blue:#003366; --oh-border:#e5e7eb; }
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:#f2f5f9; color:#111827; margin:0;}
    .wrap{max-width:820px; margin:3rem auto; padding:0 1rem;}
    .card{padding:1.25rem; background:#fff; border:1px solid var(--oh-border); border-radius:16px; box-shadow:0 8px 24px rgba(0,0,0,.06);}
    h1{margin:.25rem 0 1rem; color:var(--oh-blue); font-size:2.2rem;}
    .brand{display:flex; align-items:center; gap:.6rem}
    .brand svg{height:36px; width:36px}
    label{display:block; margin:.55rem 0 .25rem; font-weight:600;}
    input[type=file], input[type=text], input[type=password], input[type=number]{
      width:100%; padding:.8rem .9rem; border-radius:10px; border:1px solid #d1d5db; background:#fff;
    }
    .grid{display:grid; grid-template-columns:1fr 1fr; gap:1rem;}
    @media (max-width:720px){ .grid{grid-template-columns:1fr;} }
    .btn{margin-top:1rem; padding:.95rem 1.2rem; border:0; border-radius:10px; background:var(--oh-blue); color:#fff; font-weight:700; cursor:pointer;}
    .note{font-size:.95rem; color:#6b7280; margin-top:.5rem}
    .flash{background:#dcfce7; color:#166534; padding:.5rem 1rem; border-radius:8px; display:inline-block}
    .copy-btn{margin-left:.5rem; padding:.55rem .8rem; font-size:.85rem; background:#2563eb; border:none; border-radius:8px; color:#fff; cursor:pointer;}
    .footer{color:#6b7280; margin-top:1rem; text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <!-- Inline logo -->
      <svg viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg"><rect width="120" height="120" rx="16" fill="#003366"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-size="44" font-family="Arial, Helvetica, sans-serif" fill="white">OH</text></svg>
      <strong>Olde Hanter</strong>
    </div>
    <h1>Postduif van Olde Hanter</h1>

    {% with messages = get_flashed_messages() %}
      {% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}
    {% endwith %}

    <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" class="card">
      <label for="file">Bestand</label>
      <input id="file" type="file" name="file" required />

      <div class="grid" style="margin-top:.6rem">
        <div>
          <label for="expiry">Verloopt over (dagen)</label>
          <input id="expiry" type="number" name="expiry_days" step="1" min="1" placeholder="3" />
        </div>
        <div>
          <label for="pw">Wachtwoord (optioneel)</label>
          <input id="pw" type="password" name="password" placeholder="Laat leeg voor geen wachtwoord" />
        </div>
      </div>

      <button class="btn" type="submit">Uploaden</button>
      <p class="note">Max {{ max_mb }} MB per bestand.</p>
    </form>

    {% if link %}
    <div class="card" style="margin-top:1rem">
      <strong>Deelbare link:</strong>
      <div style="display:flex; gap:.5rem; align-items:center; margin-top:.35rem">
        <input type="text" id="shareLink" value="{{ link }}" readonly />
        <button class="copy-btn" onclick="copyLink()">Kopieer</button>
      </div>
      {% if pw_set %}<div class="note">Wachtwoord is ingesteld.</div>{% endif %}
    </div>
    {% endif %}

    <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
  </div>
  <script>
    function copyLink(){
      const el = document.getElementById('shareLink');
      navigator.clipboard?.writeText(el.value).then(()=>{alert('Link gekopieerd');}).catch(()=>{
        el.select(); el.setSelectionRange(0, 99999); document.execCommand('copy'); alert('Link gekopieerd');
      });
    }
  </script>
</body>
</html>
"""

PASSWORD_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Beveiligd bestand - Olde Hanter</title>
  <link rel="icon" type="image/svg+xml"
        href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 120'%3E%3Crect width='120' height='120' rx='16' fill='%23003366'/%3E%3Ctext x='50%25' y='54%25' dominant-baseline='middle' text-anchor='middle' font-size='44' font-family='Arial, Helvetica, sans-serif' fill='white'%3EOH%3C/text%3E%3C/svg%3E"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:#f2f5f9; color:#111827; margin:0;}
    .wrap{max-width:520px; margin:4rem auto; padding:2rem; background:#fff; border-radius:16px; border:1px solid #e5e7eb; box-shadow:0 8px 24px rgba(0,0,0,.06);}
    .brand{display:flex; align-items:center; gap:.6rem; margin-bottom:1rem}
    .brand svg{height:32px; width:32px}
    input{width:100%; padding:.85rem .95rem; border-radius:10px; border:1px solid #d1d5db;}
    button{margin-top:1rem; padding:.85rem 1rem; border:0; border-radius:10px; background:#003366; color:#fff; font-weight:700}
    .footer{color:#6b7280; margin-top:1rem; text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="brand">
      <svg viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg"><rect width="120" height="120" rx="16" fill="#003366"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-size="44" font-family="Arial, Helvetica, sans-serif" fill="white">OH</text></svg>
      <strong>Olde Hanter</strong>
    </div>
    <h2>Voer wachtwoord in</h2>
    {% if error %}<p style="color:#b91c1c">Onjuist wachtwoord</p>{% endif %}
    <form method="post">
      <input type="password" name="password" placeholder="Wachtwoord" required />
      <button type="submit">Ontgrendel</button>
    </form>
    <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
  </div>
</body>
</html>
"""

DOWNLOAD_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Download bestand - Olde Hanter</title>
  <link rel="icon" type="image/svg+xml"
        href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 120'%3E%3Crect width='120' height='120' rx='16' fill='%23003366'/%3E%3Ctext x='50%25' y='54%25' dominant-baseline='middle' text-anchor='middle' font-size='44' font-family='Arial, Helvetica, sans-serif' fill='white'%3EOH%3C/text%3E%3C/svg%3E"/>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:#eef2f7; color:#111827; margin:0;}
    .wrap{max-width:820px; margin:3.5rem auto; padding:0 1rem;}
    .panel{background:#fff; border:1px solid #e5e7eb; border-radius:16px; box-shadow:0 8px 24px rgba(0,0,0,.06); padding:1.5rem}
    .brand{display:flex; align-items:center; gap:.6rem; margin-bottom:1rem}
    .brand svg{height:40px; width:40px}
    .meta{margin:.5rem 0 1rem; color:#374151}
    .btn{display:inline-block; padding:.95rem 1.25rem; background:#003666; color:#fff; border-radius:10px; text-decoration:none; font-weight:700}
    .muted{color:#6b7280; font-size:.95rem}
    .linkbox{margin-top:1rem; background:#f9fafb; border:1px solid #e5e7eb; border-radius:10px; padding:.75rem}
    input[type=text]{width:100%; padding:.8rem .9rem; border-radius:10px; border:1px solid #d1d5db;}
    .copy-btn{margin-left:.5rem; padding:.55rem .8rem; font-size:.85rem; background:#2563eb; border:none; border-radius:8px; color:#fff; cursor:pointer;}
    .footer{color:#6b7280; margin-top:1rem; text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <div class="brand">
        <svg viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg"><rect width="120" height="120" rx="16" fill="#003366"/><text x="50%" y="54%" dominant-baseline="middle" text-anchor="middle" font-size="44" font-family="Arial, Helvetica, sans-serif" fill="white">OH</text></svg>
        <h1 style="margin:0">Download bestand</h1>
      </div>

      <div class="meta">
        <div><strong>Bestandsnaam:</strong> {{ name }}</div>
        <div><strong>Grootte:</strong> {{ size_human }}</div>
        <div><strong>Verloopt:</strong> {{ expires_human }}</div>
      </div>

      <a class="btn" href="{{ url_for('download_file', token=token) }}">Download</a>

      <div class="linkbox">
        <div><strong>Deelbare link</strong></div>
        <div style="display:flex; gap:.5rem; align-items:center;">
          <input type="text" id="shareLink" value="{{ share_link }}" readonly />
          <button class="copy-btn" onclick="copyLink()">Kopieer</button>
        </div>
        <div class="muted">De link blijft geldig tot de verloopdatum.</div>
      </div>

      <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
    </div>
  </div>
  <script>
    function copyLink(){
      const el = document.getElementById('shareLink');
      navigator.clipboard?.writeText(el.value).then(()=>{alert('Link gekopieerd');}).catch(()=>{
        el.select(); el.setSelectionRange(0, 99999); document.execCommand('copy'); alert('Link gekopieerd');
      });
    }
  </script>
</body>
</html>
"""

# ---------------------- Routes ----------------------
@app.route("/", methods=["GET"])
def index():
    return render_template_string(
        INDEX_HTML, link=None, pw_set=False, max_mb=MAX_CONTENT_LENGTH // (1024*1024)
    )

@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or f.filename == "":
        flash("Geen bestand geselecteerd")
        return redirect(url_for("index"))

    filename = secure_filename(f.filename)
    if not allowed_file(filename):
        flash("Bestandstype niet toegestaan")
        return redirect(url_for("index"))

    token = uuid.uuid4().hex[:10]
    stored_name = f"{token}__{filename}"
    stored_path = str((UPLOAD_DIR / stored_name).resolve())
    f.save(stored_path)

    size_bytes = Path(stored_path).stat().st_size

    # DAGEN i.p.v. uren (default 3)
    expiry_td = days_to_timedelta(request.form.get("expiry_days", "3"))
    expires_at = (datetime.now(timezone.utc) + expiry_td).isoformat()

    pw = request.form.get("password") or ""
    pw_hash = generate_password_hash(pw) if pw else None

    con = get_db()
    con.execute(
        "INSERT INTO files(token, stored_path, original_name, password_hash, expires_at, size_bytes, created_at) VALUES(?,?,?,?,?,?,?)",
        (token, stored_path, filename, pw_hash, expires_at, size_bytes, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()

    link = url_for("download", token=token, _external=True)
    return render_template_string(
        INDEX_HTML, link=link, pw_set=bool(pw), max_mb=MAX_CONTENT_LENGTH // (1024*1024)
    )

# Branded downloadpagina (met optioneel wachtwoord)
@app.route("/d/<token>", methods=["GET", "POST"])
def download(token: str):
    con = get_db()
    row = con.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    con.close()
    if not row:
        abort(404)

    now = datetime.now(timezone.utc)
    if datetime.fromisoformat(row["expires_at"]) <= now:
        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        con = get_db()
        con.execute("DELETE FROM files WHERE token=?", (token,))
        con.commit(); con.close()
        abort(410)

    if row["password_hash"]:
        if request.method == "GET":
            return render_template_string(PASSWORD_HTML, error=False)
        pw = request.form.get("password", "")
        if not check_password_hash(row["password_hash"], pw):
            return render_template_string(PASSWORD_HTML, error=True)

    # Toon branded downloadpagina
    share_link = url_for("download", token=token, _external=True)
    size_h = human_size(int(row["size_bytes"]))

    # Verlooptijd afronden op minuten
    dt = datetime.fromisoformat(row["expires_at"]).replace(second=0, microsecond=0)
    expires_h = dt.strftime("%d-%m-%Y %H:%M")

    return render_template_string(
        DOWNLOAD_HTML,
        name=row["original_name"],
        size_human=size_h,
        expires_human=expires_h,
        token=token,
        share_link=share_link,
    )

# Directe file-download
@app.route("/dl/<token>")
def download_file(token: str):
    con = get_db()
    row = con.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    con.close()
    if not row:
        abort(404)
    path = Path(row["stored_path"]).resolve()
    if not path.exists():
        abort(404)
    return send_file(path, as_attachment=True, download_name=row["original_name"])

# ---------------------- Main ----------------------
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
