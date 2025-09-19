#!/usr/bin/env python3
"""
Mini WeTransfer-like service (single-file Flask app)
Features:
- Upload large files with optional password and expiry
- Share a short download link (/d/<token>)
- Passwords stored as hashes
- Auto-cleans expired files on each request
- SQLite metadata DB

⚠️ For production: put behind a reverse proxy (nginx), use HTTPS, and consider
 object storage (e.g., S3/Backblaze), a background cleanup job, and stricter
 validation/rate-limiting.
"""
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask,
    request,
    redirect,
    url_for,
    send_file,
    abort,
    flash,
    render_template_string,
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------------------- Configuration ----------------------
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "files.db"
MAX_CONTENT_LENGTH = 512 * 1024 * 1024  # 512 MB per upload (adjust as needed)
ALLOWED_EXTENSIONS = None  # set like {"pdf","zip","jpg"} to restrict

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

@app.before_request
def cleanup_expired():
    """Delete expired files & rows on every request (simple but effective for MVP)."""
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

def hours_to_timedelta(hours_str: str) -> timedelta:
    try:
        h = float(hours_str)
        if h <= 0:
            h = 24.0
    except Exception:
        h = 24.0
    return timedelta(hours=h)

# ---------------------- Routes ----------------------

INDEX_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>MiniTransfer</title>
  <style>
    body{font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0f172a; color:#e2e8f0;}
    .wrap{max-width:780px; margin:5rem auto; padding:2rem; background:#111827; border-radius:16px; box-shadow:0 10px 30px rgba(0,0,0,.4)}
    h1{margin-top:0}
    label{display:block; margin:.75rem 0 .25rem;}
    input[type=file], input[type=text], input[type=password], input[type=number]{width:100%; padding:.75rem; border-radius:10px; border:1px solid #374151; background:#0b1220; color:#e5e7eb}
    .row{display:flex; gap:1rem}
    .row > div{flex:1}
    button{margin-top:1rem; padding:.9rem 1.1rem; border:0; border-radius:12px; background:#2563eb; color:white; font-weight:600; cursor:pointer}
    .note{font-size:.9rem; color:#93c5fd}
    .card{padding:1rem; background:#0b1220; border:1px solid #1f2937; border-radius:12px}
    a{color:#93c5fd;}
    .flash{background:#14532d; color:#d1fae5; padding:.5rem 1rem; border-radius:8px; display:inline-block}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>MiniTransfer</h1>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class="flash">{{ messages[0] }}</div>
      {% endif %}
    {% endwith %}

    <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" class="card">
      <label for="file">Bestand</label>
      <input id="file" type="file" name="file" required />

      <div class="row">
        <div>
          <label for="expiry">Verloopt over (uren)</label>
          <input id="expiry" type="number" name="expiry_hours" step="1" min="1" placeholder="24" />
        </div>
        <div>
          <label for="pw">Wachtwoord (optioneel)</label>
          <input id="pw" type="password" name="password" placeholder="Laat leeg voor geen wachtwoord" />
        </div>
      </div>

      <button type="submit">Uploaden</button>
      <p class="note">Max {{ max_mb }} MB per bestand. Link kan met wachtwoord worden beveiligd en verloopt automatisch.</p>
    </form>

    {% if link %}
    <div class="card" style="margin-top:1rem">
      <strong>Deelbare link:</strong>
      <div><a href="{{ link }}">{{ link }}</a></div>
      {% if pw_set %}<div class="note">Wachtwoord is ingesteld.</div>{% endif %}
    </div>
    {% endif %}

    <p style="margin-top:2rem; opacity:.8">Made with Flask • Bewaar geen gevoelige gegevens. Gebruik op eigen risico.</p>
  </div>
</body>
</html>
"""

PASSWORD_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Beveiligd bestand</title>
  <style>body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#0f172a; color:#e2e8f0;} .wrap{max-width:500px; margin:5rem auto; padding:2rem; background:#111827; border-radius:16px;} input{width:100%; padding:.8rem; border-radius:10px; border:1px solid #374151; background:#0b1220; color:#e5e7eb} button{margin-top:1rem; padding:.8rem 1rem; border:0; border-radius:10px; background:#2563eb; color:#fff; font-weight:600}</style>
</head>
<body>
  <div class="wrap">
    <h2>Voer wachtwoord in</h2>
    {% if error %}<p style="color:#fecaca">Onjuist wachtwoord</p>{% endif %}
    <form method="post">
      <input type="password" name="password" placeholder="Wachtwoord" required />
      <button type="submit">Ontgrendel</button>
    </form>
  </div>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return render_template_string(INDEX_HTML, link=None, pw_set=False, max_mb=MAX_CONTENT_LENGTH // (1024*1024))

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

    expiry_td = hours_to_timedelta(request.form.get("expiry_hours", "24"))
    expires_at = (datetime.now(timezone.utc) + expiry_td).isoformat()

    pw = request.form.get("password") or ""
    pw_hash = generate_password_hash(pw) if pw else None

    con = get_db()
    con.execute(
        "INSERT INTO files(token, stored_path, original_name, password_hash, expires_at, size_bytes, created_at) VALUES(?,?,?,?,?,?,?)",
        (
            token,
            stored_path,
            filename,
            pw_hash,
            expires_at,
            size_bytes,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    con.commit()
    con.close()

    link = url_for("download", token=token, _external=True)
    return render_template_string(INDEX_HTML, link=link, pw_set=bool(pw), max_mb=MAX_CONTENT_LENGTH // (1024*1024))

@app.route("/d/<token>", methods=["GET", "POST"])
def download(token: str):
    con = get_db()
    row = con.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    con.close()
    if not row:
        abort(404)

    # Check expiry
    now = datetime.now(timezone.utc)
    if datetime.fromisoformat(row["expires_at"]) <= now:
        # Clean the record/file immediately
        try:
            Path(row["stored_path"]).unlink(missing_ok=True)
        except Exception:
            pass
        con = get_db()
        con.execute("DELETE FROM files WHERE token=?", (token,))
        con.commit(); con.close()
        abort(410)  # Gone

    if row["password_hash"]:
        if request.method == "GET":
            return render_template_string(PASSWORD_HTML, error=False)
        # POST: verify password
        pw = request.form.get("password", "")
        if not check_password_hash(row["password_hash"], pw):
            return render_template_string(PASSWORD_HTML, error=True)

    path = Path(row["stored_path"]).resolve()
    if not path.exists():
        abort(404)

    # Send file with original filename
    return send_file(path, as_attachment=True, download_name=row["original_name"]) 

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
