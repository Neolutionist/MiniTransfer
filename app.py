#!/usr/bin/env python3
"""
Olde Hanter - MiniTransfer (Flask)
- Login vereist voor uploaden (1 account)
  * E-mail: info@oldehanter.nl
  * Wachtwoord: Hulsmaat
- Upload (tot 5 GB/request) met optioneel wachtwoord en verloop (in dagen, default 24)
- Map-upload: meerdere bestanden of een hele map -> server-side ZIP
- Deelbare link /d/<token> (branded downloadpagina) + /dl/<token> directe download
- Contactformulier (/contact) toont alleen 'Richtprijs' en verstuurt direct via SMTP
  (Brevo/SendGrid/Outlook/Gmail) op basis van env vars; anders nette mailto-fallback.
- SQLite metadata, lokale bestandsopslag
- Cleanup van verlopen bestanden op iedere request
"""

import os
import sqlite3
import uuid
import zipfile
import tempfile
import smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask, request, redirect, url_for, send_file, abort, flash,
    render_template_string, session
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------------------- Config ----------------------
BASE_DIR = Path(__file__).parent.resolve()
UPLOAD_DIR = BASE_DIR / "uploads"
DB_PATH = BASE_DIR / "files.db"

MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5 GB per request
ALLOWED_EXTENSIONS = None  # bv. {"pdf","zip","jpg"} om te beperken

# Login voor upload (POC)
AUTH_EMAIL = "info@oldehanter.nl"
AUTH_PASSWORD = "Hulsmaat"

# Klantprijs (alleen tonen in UI)
CUSTOMER_PRICE_PER_TB_EUR = 12.0  # pas aan naar jouw verkoopprijs per TB/maand

# SMTP config via ENV (bijv. Brevo / SendGrid / Office365 / Gmail)
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"
SMTP_FROM = os.environ.get("SMTP_FROM")  # bv. "Olde Hanter Transfer <no-reply@oldehanter.nl>"
MAIL_TO = os.environ.get("MAIL_TO", "Patrick@oldehanter.nl")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", os.urandom(16)),
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    SESSION_COOKIE_SAMESITE="Lax",
)
UPLOAD_DIR.mkdir(exist_ok=True)

# ---------------------- DB helpers ----------------------
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

# Init DB at import (werkt ook bij Gunicorn op Render)
init_db()

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
    # Default 24 dagen
    try:
        d = float(days_str)
        if d <= 0:
            d = 24.0
    except Exception:
        d = 24.0
    return timedelta(days=d)

def human_size(n: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024.0:
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}"
        n /= 1024.0
    return f"{n:.1f} PB"

def require_login():
    return bool(session.get("authed"))

def _safe_relpath(p: str) -> str:
    p = p.replace("\\", "/")
    parts = []
    for seg in p.split("/"):
        if not seg or seg in (".",):
            continue
        if seg == "..":
            continue
        parts.append(seg)
    return "/".join(parts)

def is_smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

def send_email(to_addr: str, subject: str, body: str):
    """Stuur e-mail via SMTP. Werkt met TLS (587) of SSL (465)."""
    if not is_smtp_configured():
        raise RuntimeError("SMTP is niet geconfigureerd (env vars SMTP_HOST/SMTP_USER/SMTP_PASS).")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM or SMTP_USER
    msg["To"] = to_addr
    msg.set_content(body)

    if SMTP_USE_SSL:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            if SMTP_USE_TLS:
                s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

# ---------------------- Templates ----------------------
LOGIN_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Inloggen - Olde Hanter Transfer</title>
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:480px;margin:4rem auto;padding:2rem;background:#fff;border-radius:16px;border:1px solid #e5e7eb;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    h1{margin-top:0;color:#003366}
    label{display:block;margin:.6rem 0 .25rem;font-weight:600}
    input{width:100%;padding:.85rem .95rem;border-radius:10px;border:1px solid #d1d5db}
    button{margin-top:1rem;padding:.9rem 1.2rem;border:0;border-radius:10px;background:#003366;color:#fff;font-weight:700;cursor:pointer}
    .flash{background:#fee2e2;color:#991b1b;padding:.5rem 1rem;border-radius:8px;margin-bottom:1rem}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Inloggen</h1>
    {% if error %}<div class="flash">{{ error }}</div>{% endif %}
    <form method="post" autocomplete="on">
      <label for="email">E-mail</label>
      <input id="email"
             name="email"
             type="email"
             placeholder="info@oldehanter.nl"
             value="info@oldehanter.nl"
             autocomplete="username"
             required />
      <label for="pw">Wachtwoord</label>
      <input id="pw" name="password" type="password" placeholder="Wachtwoord" required />
      <button type="submit">Inloggen</button>
    </form>
    <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
  </div>
</body>
</html>
"""

INDEX_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Olde Hanter Transfer</title>
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:900px;margin:3rem auto;padding:0 1rem}
    .card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    h1{margin:.25rem 0 1rem;color:#003366;font-size:2.1rem}
    label{display:block;margin:.55rem 0 .25rem;font-weight:600}
    input[type=file], input[type=text], input[type=password], input[type=number]{width:100%;padding:.8rem .9rem;border-radius:10px;border:1px solid #d1d5db;background:#fff}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
    @media (max-width:900px){.grid{grid-template-columns:1fr}}
    .btn{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:10px;background:#003366;color:#fff;font-weight:700;cursor:pointer}
    .note{font-size:.95rem;color:#6b7280;margin-top:.5rem}
    .flash{background:#dcfce7;color:#166534;padding:.5rem 1rem;border-radius:8px;display:inline-block}
    .copy-btn{margin-left:.5rem;padding:.55rem .8rem;font-size:.85rem;background:#2563eb;border:none;border-radius:8px;color:#fff;cursor:pointer}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
    .topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:.5rem}
    .logout{font-size:.95rem}
    .logout a{color:#003366;text-decoration:none;font-weight:700}
    .row{display:flex;align-items:center;gap:.6rem}
    .hint{font-size:.9rem;color:#6b7280}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <h1>Bestanden delen met Olde Hanter</h1>
      <div class="logout">Ingelogd als {{ user_email }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
    </div>

    {% with messages = get_flashed_messages() %}
      {% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}
    {% endwith %}

    <form method="post" action="{{ url_for('upload') }}" enctype="multipart/form-data" class="card" autocomplete="off">
      <label for="file">Bestanden of map</label>
      <input id="file" type="file" name="file" multiple required />
      <div class="row">
        <input type="checkbox" id="folderMode" name="folder_mode" value="1" />
        <label for="folderMode" style="margin:0">Map uploaden</label>
        <span class="hint">(zet de kiezer in map-modus; we maken er automatisch één .zip van)</span>
      </div>

      <div class="grid" style="margin-top:.6rem">
        <div>
          <label for="expiry">Verloopt over (dagen)</label>
          <input id="expiry" type="number" name="expiry_days" step="1" min="1" placeholder="24" />
        </div>
        <div>
          <label for="pw">Wachtwoord (optioneel)</label>
          <input id="pw"
                 type="password"
                 name="password"
                 placeholder="Laat leeg voor geen wachtwoord"
                 autocomplete="new-password"
                 autocapitalize="off"
                 spellcheck="false" />
        </div>
      </div>

      <button class="btn" type="submit">Uploaden</button>
      <p class="note">Max {{ max_mb }} MB per request (bij map-upload telt alles samen).</p>
    </form>

    {% if link %}
    <div class="card" style="margin-top:1rem">
      <strong>Deelbare link:</strong>
      <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
        <input type="text" id="shareLink" value="{{ link }}" readonly />
        <button class="copy-btn" onclick="copyLink()">Kopieer</button>
      </div>
      {% if pw_set %}<div class="note">Wachtwoord is ingesteld.</div>{% endif %}
    </div>
    {% endif %}

    <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
  </div>
  <script>
    // Zet file input in directory-modus als "Map uploaden" is aangevinkt
    const folderCb = document.getElementById('folderMode');
    const fileInput = document.getElementById('file');
    function applyFolderMode(){
      if(folderCb.checked){
        fileInput.setAttribute('webkitdirectory','');
        fileInput.setAttribute('directory','');
      }else{
        fileInput.removeAttribute('webkitdirectory');
        fileInput.removeAttribute('directory');
      }
    }
    folderCb.addEventListener('change', applyFolderMode);
    applyFolderMode();

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
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:520px;margin:4rem auto;padding:2rem;background:#fff;border-radius:16px;border:1px solid #e5e7eb;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    input{width:100%;padding:.85rem .95rem;border-radius:10px;border:1px solid #d1d5db}
    button{margin-top:1rem;padding:.85rem 1rem;border:0;border-radius:10px;background:#003366;color:#fff;font-weight:700}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
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
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#eef2f7;color:#111827;margin:0}
    .wrap{max-width:820px;margin:3.5rem auto;padding:0 1rem}
    .panel{background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06);padding:1.5rem}
    .meta{margin:.5rem 0 1rem;color:#374151}
    .btn{display:inline-block;padding:.95rem 1.25rem;background:#003366;color:#fff;border-radius:10px;text-decoration:none;font-weight:700}
    .muted{color:#6b7280;font-size:.95rem}
    .linkbox{margin-top:1rem;background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:.75rem}
    input[type=text]{width:100%;padding:.8rem .9rem;border-radius:10px;border:1px solid #d1d5db}
    .copy-btn{margin-left:.5rem;padding:.55rem .8rem;font-size:.85rem;background:#2563eb;border:none;border-radius:8px;color:#fff;cursor:pointer}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
    .contact{margin-top:1rem;text-align:center}
    .contact a{display:inline-block;margin-top:.5rem;padding:.7rem 1rem;border-radius:10px;background:#e5e7eb;color:#111827;text-decoration:none;font-weight:700}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="panel">
      <h1>Download bestand</h1>
      <div class="meta">
        <div><strong>Bestandsnaam:</strong> {{ name }}</div>
        <div><strong>Grootte:</strong> {{ size_human }}</div>
        <div><strong>Verloopt:</strong> {{ expires_human }}</div>
      </div>
      <a class="btn" href="{{ url_for('download_file', token=token) }}">Download</a>

      <div class="linkbox">
        <div><strong>Deelbare link</strong></div>
        <div style="display:flex;gap:.5rem;align-items:center;">
          <input type="text" id="shareLink" value="{{ share_link }}" readonly />
          <button class="copy-btn" onclick="copyLink()">Kopieer</button>
        </div>
        <div class="muted">De link blijft geldig tot de verloopdatum.</div>
      </div>

      <div class="contact">
        <div>Interesse in een eigen transfer-oplossing?</div>
        <a href="{{ url_for('contact') }}">Vraag offerte / stel samen</a>
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

CONTACT_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Eigen transfer-oplossing - Olde Hanter</title>
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:720px;margin:3rem auto;padding:0 1rem}
    .card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    h1{margin:.25rem 0 1rem;color:#003366}
    label{display:block;margin:.55rem 0 .25rem;font-weight:600}
    input, select{width:100%;padding:.85rem .95rem;border-radius:10px;border:1px solid #d1d5db;background:#fff}
    .grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
    @media (max-width:720px){.grid{grid-template-columns:1fr}}
    .btn{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:10px;background:#003366;color:#fff;font-weight:700;cursor:pointer}
    .note{font-size:.95rem;color:#6b7280;margin-top:.5rem}
    .price{font-weight:800}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Eigen transfer-oplossing aanvragen</h1>
      <form method="post" action="{{ url_for('contact') }}">
        <div class="grid">
          <div>
            <label for="login_email">Gewenste inlog-e-mail</label>
            <input id="login_email" name="login_email" type="email" placeholder="naam@bedrijf.nl" required />
          </div>
          <div>
            <label for="storage_tb">Gewenste opslaggrootte</label>
            <select id="storage_tb" name="storage_tb" required onchange="updatePrice()">
              <option value="0.5">0,5 TB</option>
              <option value="1" selected>1 TB</option>
              <option value="2">2 TB</option>
              <option value="5">5 TB</option>
            </select>
          </div>
        </div>

        <div class="grid" style="margin-top:1rem">
          <div>
            <label for="company">Bedrijfsnaam</label>
            <input id="company" name="company" type="text" placeholder="Bedrijfsnaam BV" />
          </div>
          <div>
            <label for="phone">Telefoonnummer</label>
            <input id="phone" name="phone" type="tel" placeholder="+31 6 12345678" />
          </div>
        </div>

        <p class="note">
          Richtprijs: <span class="price">€<span id="cost_offer">{{ offer_cost_eur }}</span>/maand</span>
          (op basis van <span id="tb_val">1</span> TB).
        </p>

        <button class="btn" type="submit">Verstuur aanvraag</button>
      </form>
      <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
    </div>
  </div>
  <script>
    let baseOffer = {{ base_offer }};
    const storageSel = document.getElementById('storage_tb');
    const offerEl = document.getElementById('cost_offer');
    const tbVal = document.getElementById('tb_val');
    function updatePrice(){
      const tb = parseFloat(storageSel.value || "1");
      const offer = tb * baseOffer;
      offerEl.textContent = offer.toFixed(2).replace('.', ',');
      tbVal.textContent = tb.toString().replace('.', ',');
    }
    updatePrice();
  </script>
</body>
</html>
"""

CONTACT_DONE_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Aanvraag verstuurd</title>
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:680px;margin:3rem auto;padding:0 1rem}
    .card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Aanvraag is verstuurd</h1>
      <p>Bedankt! We nemen zo snel mogelijk contact met je op.</p>
      <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
    </div>
  </div>
</body>
</html>
"""

CONTACT_MAIL_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Aanvraag klaarzetten</title>
  <style>
    *,*::before,*::after{box-sizing:border-box}
    body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;color:#111827;margin:0}
    .wrap{max-width:680px;margin:3rem auto;padding:0 1rem}
    .card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
    .btn{display:inline-block;margin-top:1rem;padding:.95rem 1.2rem;border-radius:10px;background:#00366
0;color:#fff;text-decoration:none;font-weight:700}
    .footer{color:#6b7280;margin-top:1rem;text-align:center}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Aanvraag gereed</h1>
      <p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p>
      <a class="btn" href="{{ mailto_link }}">Open e-mail</a>
      <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
    </div>
  </div>
</body>
</html>
"""

# ---------------------- Routes ----------------------
@app.route("/", methods=["GET"])
def home():
    if not require_login():
        return redirect(url_for("login"))
    return render_template_string(
        INDEX_HTML,
        link=None,
        pw_set=False,
        max_mb=MAX_CONTENT_LENGTH // (1024*1024),
        user_email=session.get("user_email", AUTH_EMAIL),
    )

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        password = request.form.get("password") or ""
        if email.lower() == AUTH_EMAIL.lower() and password == AUTH_PASSWORD:
            session["authed"] = True
            session["user_email"] = email
            return redirect(url_for("home"))
        return render_template_string(LOGIN_HTML, error="Onjuiste inloggegevens.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/upload", methods=["POST"])
def upload():
    if not require_login():
        return redirect(url_for("login"))

    # Meerdere files mogelijk (ook directory-mode)
    files = request.files.getlist("file")
    if not files or all(f.filename == "" for f in files):
        flash("Geen bestand geselecteerd")
        return redirect(url_for("home"))

    folder_mode = request.form.get("folder_mode") == "1"
    expiry_td = days_to_timedelta(request.form.get("expiry_days", "24"))
    expires_at = (datetime.now(timezone.utc) + expiry_td).isoformat()
    pw = request.form.get("password") or ""
    pw_hash = generate_password_hash(pw) if pw else None

    token = uuid.uuid4().hex[:10]

    # Meerdere bestanden of folder-modus -> ZIP
    if len(files) > 1 or folder_mode:
        first_name = files[0].filename or "map"
        first_name = _safe_relpath(first_name)
        top = first_name.split("/", 1)[0] if "/" in first_name else (first_name or "map")
        top = secure_filename(top) or "map"

        zip_name = f"{token}__{top}.zip"
        zip_path = (UPLOAD_DIR / zip_name).resolve()

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                if f and f.filename:
                    rel = _safe_relpath(f.filename)
                    if not rel:
                        rel = secure_filename(f.filename)
                    with tempfile.NamedTemporaryFile(delete=True) as tmp:
                        f.save(tmp.name)
                        zf.write(tmp.name, arcname=rel)

        stored_path = str(zip_path)
        original_name = zip_path.name
        size_bytes = zip_path.stat().st_size
    else:
        f = files[0]
        if f.filename == "":
            flash("Ongeldig bestand")
            return redirect(url_for("home"))

        filename = secure_filename(f.filename)
        if not allowed_file(filename):
            flash("Bestandstype niet toegestaan")
            return redirect(url_for("home"))

        stored_name = f"{token}__{filename}"
        stored_path = str((UPLOAD_DIR / stored_name).resolve())
        f.save(stored_path)
        original_name = filename
        size_bytes = Path(stored_path).stat().st_size

    con = get_db()
    con.execute(
        "INSERT INTO files(token, stored_path, original_name, password_hash, expires_at, size_bytes, created_at) VALUES(?,?,?,?,?,?,?)",
        (token, stored_path, original_name, pw_hash, expires_at, size_bytes, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()

    link = url_for("download", token=token, _external=True)
    return render_template_string(
        INDEX_HTML,
        link=link,
        pw_set=bool(pw),
        max_mb=MAX_CONTENT_LENGTH // (1024*1024),
        user_email=session.get("user_email", AUTH_EMAIL),
    )

@app.route("/d/<token>", methods=["GET", "POST"])
def download(token: str):
    con = get_db()
    row = con.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    con.close()
    if not row:
        abort(404)

    # expiry check
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

    # wachtwoord?
    if row["password_hash"]:
        if request.method == "GET":
            return render_template_string(PASSWORD_HTML, error=False)
        pw = request.form.get("password", "")
        if not check_password_hash(row["password_hash"], pw):
            return render_template_string(PASSWORD_HTML, error=True)

    share_link = url_for("download", token=token, _external=True)
    size_h = human_size(int(row["size_bytes"]))
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

@app.route("/contact", methods=["GET", "POST"])
def contact():
    if request.method == "GET":
        offer_cost = 1.0 * CUSTOMER_PRICE_PER_TB_EUR
        return render_template_string(
            CONTACT_HTML,
            base_offer=CUSTOMER_PRICE_PER_TB_EUR,
            offer_cost_eur=f"{offer_cost:.2f}".replace('.', ','),
        )

    # POST: SMTP direct (of nette mailto fallback)
    login_email = (request.form.get("login_email") or "").strip()
    try:
        storage_tb = float(request.form.get("storage_tb") or "1")
    except Exception:
        storage_tb = 1.0
    company = (request.form.get("company") or "").strip()
    phone = (request.form.get("phone") or "").strip()

    subject = "Nieuwe aanvraag transfer-oplossing"
    body = (
        "Er is een nieuwe aanvraag binnengekomen voor een eigen transfer-oplossing:\n\n"
        f"- Gewenste inlog-e-mail: {login_email}\n"
        f"- Gewenste opslag: {storage_tb} TB\n"
        f"- Bedrijfsnaam: {company}\n"
        f"- Telefoonnummer: {phone}\n"
    )

    if is_smtp_configured():
        try:
            send_email(MAIL_TO, subject, body)
            return render_template_string(CONTACT_DONE_HTML)
        except Exception:
            mailto = f"mailto:{MAIL_TO}?subject={quote(subject)}&body={quote(body)}"
            return render_template_string(CONTACT_MAIL_HTML, mailto_link=mailto)
    else:
        mailto = f"mailto:{MAIL_TO}?subject={quote(subject)}&body={quote(body)}"
        return render_template_string(CONTACT_MAIL_HTML, mailto_link=mailto)

# ---------------------- Main ----------------------
if __name__ == "__main__":
    # (init_db() is al bij import aangeroepen)
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
