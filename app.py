#!/usr/bin/env python3
"""
Mini WeTransfer-like service (Flask app)
Features:
- Upload large files with optional password and expiry
- Share a short download link (/d/<token>)
- Passwords stored as hashes
- Auto-cleans expired files on each request
- SQLite metadata DB
- Styled for Olde Hanter with copy-to-clipboard button, favicon, and branded download page
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

BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "files.db"
MAX_CONTENT_LENGTH = 512 * 1024 * 1024  # 512 MB
ALLOWED_EXTENSIONS = None

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

def human_size(n: int) -> str:
    for unit in ['B','KB','MB','GB','TB']:
        if n < 1024.0:
            return f"{n:.1f} {unit}" if unit != 'B' else f"{n} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

# ---------------------- Templates ----------------------

INDEX_HTML = """
<!doctype html>
<html lang=\"nl\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Olde Hanter Transfer</title>
  <link rel=\"icon\" type=\"image/svg+xml\" href=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" />
  <style>
    body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#f9fafb; color:#111827;}
    .wrap{max-width:780px; margin:3rem auto; padding:2rem; background:#ffffff; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.1);} 
    h1{margin-top:0; color:#003366;}
    label{display:block; margin:.75rem 0 .25rem; font-weight:600;}
    input[type=file], input[type=text], input[type=password], input[type=number]{width:100%; padding:.75rem; border-radius:8px; border:1px solid #d1d5db;}
    .row{display:flex; gap:1rem;}
    .row > div{flex:1}
    button{margin-top:1rem; padding:.9rem 1.1rem; border:0; border-radius:8px; background:#003366; color:white; font-weight:600; cursor:pointer}
    .note{font-size:.9rem; color:#555;}
    .card{padding:1rem; background:#f3f4f6; border:1px solid #e5e7eb; border-radius:8px;}
    a{color:#003366; font-weight:600;}
    .flash{background:#dcfce7; color:#166534; padding:.5rem 1rem; border-radius:6px; display:inline-block}
    .copy-btn{margin-left:.5rem; padding:.3rem .7rem; font-size:.8rem; background:#2563eb; border:none; border-radius:6px; color:#fff; cursor:pointer;}
  </style>
</head>
<body>
  <div class=\"wrap\"> 
    <img src=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" alt=\"Olde Hanter\" style=\"max-height:60px; margin-bottom:1rem\"/>
    <h1>Bestanden delen met Olde Hanter</h1>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        <div class=\"flash\">{{ messages[0] }}</div>
      {% endif %}
    {% endwith %}

    <form method=\"post\" action=\"{{ url_for('upload') }}\" enctype=\"multipart/form-data\" class=\"card\">
      <label for=\"file\">Bestand</label>
      <input id=\"file\" type=\"file\" name=\"file\" required />

      <div class=\"row\"> 
        <div>
          <label for=\"expiry\">Verloopt over (uren)</label>
          <input id=\"expiry\" type=\"number\" name=\"expiry_hours\" step=\"1\" min=\"1\" placeholder=\"24\" />
        </div>
        <div>
          <label for=\"pw\">Wachtwoord (optioneel)</label>
          <input id=\"pw\" type=\"password\" name=\"password\" placeholder=\"Laat leeg voor geen wachtwoord\" />
        </div>
      </div>

      <button type=\"submit\">Uploaden</button>
      <p class=\"note\">Max {{ max_mb }} MB per bestand.</p>
    </form>

    {% if link %}
    <div class=\"card\" style=\"margin-top:1rem\">
      <strong>Deelbare link:</strong>
      <div>
        <input type=\"text\" id=\"shareLink\" value=\"{{ link }}\" readonly style=\"width:80%\"/>
        <button class=\"copy-btn\" onclick=\"copyLink()\">Kopieer</button>
      </div>
      {% if pw_set %}<div class=\"note\">Wachtwoord is ingesteld.</div>{% endif %}
    </div>
    {% endif %}

    <p style=\"margin-top:2rem; opacity:.8\">Olde Hanter Bouwconstructies • Bestandentransfer</p>
  </div>
  <script>
    function copyLink() {
      var copyText = document.getElementById(\"shareLink\");
      copyText.select();
      copyText.setSelectionRange(0, 99999);
      document.execCommand(\"copy\");
      alert(\"Link gekopieerd: \" + copyText.value);
    }
  </script>
</body>
</html>
"""

PASSWORD_HTML = """
<!doctype html>
<html lang=\"nl\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Beveiligd bestand - Olde Hanter</title>
  <link rel=\"icon\" type=\"image/svg+xml\" href=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" />
  <style>body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#f9fafb; color:#111827;} .wrap{max-width:500px; margin:5rem auto; padding:2rem; background:#fff; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.1);} input{width:100%; padding:.8rem; border-radius:8px; border:1px solid #d1d5db;} button{margin-top:1rem; padding:.8rem 1rem; border:0; border-radius:8px; background:#003366; color:#fff; font-weight:600;}</style>
</head>
<body>
  <div class=\"wrap\"> 
    <img src=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" alt=\"Olde Hanter\" style=\"max-height:50px; margin-bottom:1rem\"/>
    <h2>Voer wachtwoord in</h2>
    {% if error %}<p style=\"color:#b91c1c\">Onjuist wachtwoord</p>{% endif %}
    <form method=\"post\"> 
      <input type=\"password\" name=\"password\" placeholder=\"Wachtwoord\" required />
      <button type=\"submit\">Ontgrendel</button>
    </form>
  </div>
</body>
</html>
"""

DOWNLOAD_HTML = """
<!doctype html>
<html lang=\"nl\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Download bestand - Olde Hanter</title>
  <link rel=\"icon\" type=\"image/svg+xml\" href=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" />
  <style>
    body{font-family:system-ui, -apple-system, Segoe UI, Roboto, sans-serif; background:#f3f4f6; color:#111827;}
    .wrap{max-width:720px; margin:4rem auto; padding:2rem; background:#ffffff; border-radius:12px; box-shadow:0 4px 20px rgba(0,0,0,.1);} 
    .header{display:flex; align-items:center; gap:12px; margin-bottom:1rem}
    .header img{max-height:48px}
    .meta{margin:.5rem 0 1rem; color:#374151}
    .btn{display:inline-block; padding:.9rem 1.2rem; background:#003366; color:#fff; border-radius:8px; text-decoration:none; font-weight:700}
    .muted{color:#6b7280; font-size:.9rem}
    .linkbox{margin-top:1rem; background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px; padding:.75rem}
    .copy-btn{margin-left:.5rem; padding:.3rem .7rem; font-size:.8rem; background:#2563eb; border:none; border-radius:6px; color:#fff; cursor:pointer;}
  </style>
</head>
<body>
  <div class=\"wrap\">
    <div class=\"header\">
      <img src=\"https://www.oldehanter.nl/wp-content/uploads/2021/03/Logo-olde-hanter.svg\" alt=\"Olde Hanter\">
      <h1 style=\"margin:0\">Download bestand</h1>
    </div>

    <div class=\"meta\">
      <div><strong>Bestandsnaam:</strong> {{ name }}</div>
      <div><strong>Grootte:</strong> {{ size_human }}</div>
      <div><strong>Verloopt:</strong> {{ expires_human }}</div>
    </div>

    <a class=\"btn\" href=\"{{ url_for('download_file', token=token) }}\">Download</a>

    <div class=\"linkbox\">
      <div><strong>Deelbare link</strong></div>
      <div>
        <input type=\"text\" id=\"shareLink\" value=\"{{ share_link }}\" readonly style=\"width:80%\"/>
        <button class=\"copy-btn\" onclick=\"copyLink()\">Kopieer</button>
      </div>
      <div class=\"muted\">De link blijft geldig tot de verloopdatum.</div>
    </div>
  </div>
  <script>
    function copyLink() {
      var copyText = document.getElementById('shareLink');
      copyText.select();
      copyText.setSelectionRange(0, 99999);
      document.execCommand('copy');
      alert('Link gekopieerd: ' + copyText.value);
    }
  </script>
</body>
</html>
"""

# ---------------------- Routes ----------------------

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

    # Show branded download page instead of direct download
    share_link = url_for("download", token=token, _external=True)
    size_h = human_size(int(row["size_bytes"]))
    expires_h = row["expires_at"].replace("T", " ")
    return render_template_string(
        DOWNLOAD_HTML,
        name=row["original_name"],
        size_human=size_h,
        expires_human=expires_h,
        token=token,
        share_link=share_link,
    )

# Separate endpoint that sends the file (used by the button on the page)
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


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
