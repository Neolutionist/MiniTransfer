#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sqlite3, uuid, tempfile, zipfile, smtplib, re
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, abort, render_template_string,
    session, jsonify
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

import boto3
from botocore.config import Config as BotoConfig

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

SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
MAIL_TO   = os.environ.get("MAIL_TO", "Patrick@oldehanter.nl")

def smtp_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

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

# -------------- gedeelde CSS --------------
BASE_CSS = """
*,*:before,*:after{box-sizing:border-box}
:root{
  --bg1:#60a5fa; --bg2:#a78bfa; --bg3:#34d399;
  --panel:rgba(255,255,255,.82); --panel-b:rgba(255,255,255,.45);
  --brand:#003366; --text:#0f172a;
}
html,body{height:100%}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);margin:0;position:relative;overflow-x:hidden}
.bg{position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vmax 60vmax at 15% 25%,var(--bg1) 0%,transparent 60%),
  radial-gradient(55vmax 55vmax at 85% 20%,var(--bg2) 0%,transparent 60%),
  radial-gradient(60vmax 60vmax at 50% 90%,var(--bg3) 0%,transparent 60%),
  linear-gradient(180deg,#eef2f7 0%,#e9eef6 100%)}
.bg:before,.bg:after{content:"";position:absolute;inset:-8%;will-change:transform}
.bg:before{background:
  radial-gradient(40% 60% at 20% 30%,rgba(255,255,255,.35),transparent),
  radial-gradient(50% 60% at 80% 25%,rgba(255,255,255,.25),transparent);
  animation:f1 16s linear infinite}
.bg:after{background:
  radial-gradient(35% 50% at 60% 70%,rgba(255,255,255,.22),transparent),
  radial-gradient(45% 55% at 30% 80%,rgba(255,255,255,.18),transparent);
  animation:f2 22s linear infinite}
@keyframes f1{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(1.5%,-1.5%,0) rotate(180deg)}100%{transform:translate3d(0,0,0) rotate(360deg)}}
@keyframes f2{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(-1.25%,1.25%,0) rotate(-180deg)}100%{transform:translate3d(0,0,0) rotate(-360deg)}}
@media (prefers-reduced-motion:reduce){.bg:before,.bg:after{animation:none}}
.wrap{max-width:980px;margin:6vh auto;padding:0 1rem}
.card{padding:1.5rem;background:var(--panel);border:1px solid var(--panel-b);
      border-radius:18px;box-shadow:0 18px 40px rgba(0,0,0,.12);backdrop-filter: blur(10px)}
.btn{padding:.95rem 1.2rem;border:0;border-radius:12px;background:var(--brand);color:#fff;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(0,51,102,.25)}
.footer{color:#334155;margin-top:1.2rem;text-align:center}
label{display:block;margin:.55rem 0 .25rem;font-weight:600}
input[type=file],input[type=number],input[type=password],input[type=text],select{
  width:100%;padding:.9rem 1rem;border-radius:12px;border:1px solid #d1d5db;background:#fff}
"""

# -------------- Templates (géén f-strings) --------------
LOGIN_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen – Olde Hanter</title>
<style>
{{ base_css }}
.wrap{max-width:520px}
h1{color:var(--brand);margin:0 0 1rem}
</style></head><body>
<div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card">
  <h1>Inloggen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}
  <form method="post" autocomplete="on">
    <label for="email">E-mail</label>
    <input id="email" name="email" type="email" value="info@oldehanter.nl" autocomplete="username" required>
    <label for="pw">Wachtwoord</label>
    <input id="pw" name="password" type="password" placeholder="Wachtwoord" autocomplete="current-password" required>
    <button class="btn" type="submit" style="margin-top:1rem">Inloggen</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

INDEX_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Olde Hanter – Upload</title>
<style>
{{ base_css }}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
h1{margin:.25rem 0 1rem;color:var(--brand);font-size:2.1rem}
.logout a{color:var(--brand);text-decoration:none;font-weight:700}
.note{font-size:.95rem;color:#334155;margin-top:.5rem}
</style></head><body>
<div class="bg" aria-hidden="true"></div>

<div class="wrap">
  <div class="topbar">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div class="logout">Ingelogd als {{ user }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  <form id="f" class="card" enctype="multipart/form-data" autocomplete="off">
    <label for="files">Bestand of map</label>
    <input id="files" type="file" name="files" multiple webkitdirectory directory>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.6rem">
      <div>
        <label for="exp">Verloopt over (dagen)</label>
        <input id="exp" type="number" name="expiry_days" min="1" value="24">
      </div>
      <div>
        <label for="pw">Wachtwoord (optioneel)</label>
        <input id="pw" type="password" name="password" placeholder="Laat leeg voor geen wachtwoord" autocomplete="new-password" autocapitalize="off" spellcheck="false">
      </div>
    </div>

    <button class="btn" type="submit" style="margin-top:1rem">Uploaden</button>
    <p class="note">Je kunt één bestand of een hele map selecteren. Mapuploads worden automatisch als ZIP gebundeld.
      Max {{ max_mb }} MB via server-relay.</p>
  </form>

  <div id="result"></div>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  const form = document.getElementById('f');
  const input = document.getElementById('files');
  const resBox = document.getElementById('result');

  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const files = input.files;
    if(!files || files.length===0){ alert("Kies een bestand of map"); return; }

    const fd = new FormData();
    fd.append('expiry_days', document.getElementById('exp').value || '24');
    fd.append('password', document.getElementById('pw').value || '');

    for(const f of files){
      fd.append('files', f, f.name);
      fd.append('paths', f.webkitRelativePath || f.name);
    }

    let res;
    try{ res = await fetch("{{ url_for('upload_relay') }}", { method:"POST", body: fd }); }
    catch(e){ alert("Verbinding met server mislukt."); return; }

    const data = await res.json().catch(()=>({ok:false,error:"Onbekende fout"}));
    if(!res.ok || !data.ok){ alert(data.error || ("Fout: "+res.status)); return; }

    resBox.innerHTML = `
      <div class="card" style="margin-top:1rem">
        <strong>Deelbare link</strong>
        <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
          <input style="flex:1;padding:.8rem;border-radius:10px;border:1px solid #d1d5db" value="${data.link}" readonly>
          <button class="btn" type="button"
            onclick="(navigator.clipboard?.writeText('${data.link}')||Promise.reject()).then(()=>alert('Link gekopieerd'))">
            Kopieer
          </button>
        </div>
      </div>`;
  });
</script>
</body></html>
"""

DOWNLOAD_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Download – Olde Hanter</title>
<style>
{{ base_css }}
h1{margin:.2rem 0 1rem;color:var(--brand)}
.meta{margin:.4rem 0 1rem;color:#374151}
.btn{padding:.9rem 1.15rem;border-radius:12px;background:var(--brand);color:#fff;text-decoration:none;font-weight:700}
.linkbox{margin-top:1rem;background:rgba(255,255,255,.65);border:1px solid rgba(255,255,255,.35);border-radius:12px;padding:.9rem}
input[type=text]{width:100%;padding:.8rem .9rem;border-radius:10px;border:1px solid #d1d5db;background:#fff}
.actions{display:flex;gap:.6rem;flex-wrap:wrap;margin-top:1rem}
.secondary{background:#0f4c98}
</style></head><body>
<div class="bg" aria-hidden="true"></div>

<div class="wrap"><div class="card">
  <h1>Download bestand</h1>
  <div class="meta">
    <div><strong>Bestandsnaam:</strong> {{ name }}</div>
    <div><strong>Grootte:</strong> {{ size_human }}</div>
    <div><strong>Verloopt:</strong> {{ expires_human }}</div>
  </div>

  <div class="actions">
    <a class="btn" href="{{ url_for('download_file', token=token) }}">Download</a>
    <a class="btn secondary" href="{{ url_for('contact') }}">Eigen transfer-oplossing aanvragen</a>
  </div>

  <div class="linkbox">
    <div><strong>Deelbare link</strong></div>
    <div style="display:flex;gap:.5rem;align-items:center;">
      <input type="text" id="shareLink" value="{{ share_link }}" readonly>
      <button class="btn" type="button"
        onclick="(navigator.clipboard?.writeText('{{ share_link }}')||Promise.reject()).then(()=>alert('Link gekopieerd'))">
        Kopieer
      </button>
    </div>
  </div>

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

CONTACT_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eigen transfer-oplossing – Olde Hanter</title>
<style>
{{ base_css }}
.wrap{max-width:720px}
.note{font-size:.95rem;color:#6b7280;margin-top:.5rem}
.error{background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem}
</style></head><body>
<div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card">
  <h1>Eigen transfer-oplossing aanvragen</h1>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('contact') }}" novalidate>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label for="login_email">Gewenste inlog-e-mail</label>
        <input id="login_email" name="login_email" type="email" placeholder="naam@bedrijf.nl" value="{{ form.login_email or '' }}" required>
      </div>
      <div>
        <label for="storage_tb">Gewenste opslaggrootte</label>
        <select id="storage_tb" name="storage_tb" required>
          <option value="">Maak een keuze…</option>
          <option value="0.5" {% if form.storage_tb=='0.5' %}selected{% endif %}>0,5 TB — €6/maand</option>
          <option value="1"   {% if (form.storage_tb or '1')=='1' %}selected{% endif %}>1 TB — €12/maand</option>
          <option value="2"   {% if form.storage_tb=='2' %}selected{% endif %}>2 TB — €24/maand</option>
          <option value="5"   {% if form.storage_tb=='5' %}selected{% endif %}>5 TB — €60/maand</option>
        </select>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:1rem">
      <div>
        <label for="company">Bedrijfsnaam</label>
        <input id="company" name="company" type="text" placeholder="Bedrijfsnaam BV" value="{{ form.company or '' }}" minlength="2" maxlength="100" required>
      </div>
      <div>
        <label for="phone">Telefoonnummer</label>
        <input id="phone" name="phone" type="tel" placeholder="+31 6 12345678" value="{{ form.phone or '' }}" pattern="^[0-9+()\\s-]{8,20}$" required>
      </div>
    </div>
    <p class="note">Richtprijs wordt op basis van je keuze meegestuurd in de e-mail.</p>
    <button class="btn" type="submit" style="margin-top:1rem">Verstuur aanvraag</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

CONTACT_DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag verstuurd</title>
<style>{{ base_css }}.wrap{max-width:720px}</style>
</head><body><div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card"><h1>Dank je wel!</h1><p>Je aanvraag is verstuurd. We nemen zo snel mogelijk contact met je op.</p><p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div></body></html>
"""

CONTACT_MAIL_FALLBACK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title>
<style>{{ base_css }}.wrap{max-width:720px}</style>
</head><body><div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card">
  <h1>Aanvraag gereed</h1>
  <p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p>
  <a class="btn" href="{{ mailto_link }}">Open e-mail</a>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div></body></html>
"""

# -------------- Helpers --------------
def logged_in() -> bool:
    return session.get("authed", False)

def human(n: int) -> str:
    x = float(n)
    for u in ["B","KB","MB","GB","TB"]:
        if x < 1024 or u == "TB":
            return f"{x:.1f} {u}" if u!="B" else f"{int(x)} {u}"
        x /= 1024

def send_email(to_addr: str, subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
        s.starttls()
        s.login(SMTP_USER, SMTP_PASS)
        s.send_message(msg)

# -------------- Routes --------------
@app.route("/")
def index():
    if not logged_in():
        return redirect(url_for("login"))
    return render_template_string(INDEX_HTML, user=session.get("user"), max_mb=MAX_RELAY_MB, base_css=BASE_CSS)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if (request.form.get("email") or "").lower()==AUTH_EMAIL and request.form.get("password")==AUTH_PASSWORD:
            session["authed"] = True
            session["user"] = AUTH_EMAIL
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="Onjuiste inloggegevens.", base_css=BASE_CSS)
    return render_template_string(LOGIN_HTML, error=None, base_css=BASE_CSS)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# Browser → server → B2 (geen CORS) + map→ZIP bundeling
@app.route("/upload-relay", methods=["POST"])
def upload_relay():
    if not logged_in():
        return abort(401)

    files = request.files.getlist("files")
    paths = request.form.getlist("paths")
    if not files:
        return jsonify(ok=False, error="Geen bestand"), 400

    if request.content_length and request.content_length > MAX_RELAY_BYTES:
        return jsonify(ok=False, error=f"Upload groter dan {MAX_RELAY_MB} MB"), 413

    expiry_days = float(request.form.get("expiry_days") or 24)
    password = request.form.get("password") or ""
    pw_hash = generate_password_hash(password) if password else None

    must_zip = len(files) > 1 or any("/" in (p or "") for p in paths)
    token = uuid.uuid4().hex[:10]

    if must_zip:
        root = None
        for p in paths:
            if "/" in p:
                root = p.split("/", 1)[0]
                break
        zip_name = f"{root or 'map-upload'}_{token}.zip"
        object_key = f"uploads/{token}__{zip_name}"

        tmp_path = Path(tempfile.gettempdir()) / f"bundle_{token}.zip"
        try:
            with zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=7) as z:
                for f, p in zip(files, paths):
                    arc = p or f.filename
                    with z.open(arc, "w") as dest:
                        while True:
                            chunk = f.stream.read(1024 * 1024)
                            if not chunk: break
                            dest.write(chunk)

            s3.upload_file(str(tmp_path), S3_BUCKET, object_key)
            size_bytes = tmp_path.stat().st_size
        finally:
            try: tmp_path.unlink(missing_ok=True)
            except: pass

        original_name = zip_name

    else:
        f = files[0]
        filename = secure_filename(f.filename)
        object_key = f"uploads/{token}__{filename}"
        s3.upload_fileobj(f.stream, S3_BUCKET, object_key)
        head = s3.head_object(Bucket=S3_BUCKET, Key=object_key)
        size_bytes = int(head["ContentLength"])
        original_name = filename

    expires_at = (datetime.now(timezone.utc) + timedelta(days=expiry_days)).isoformat()

    c = db()
    c.execute("""INSERT INTO files(token,stored_path,original_name,password_hash,expires_at,size_bytes,created_at)
                 VALUES(?,?,?,?,?,?,?)""",
              (token, object_key, original_name, pw_hash, expires_at, size_bytes, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()

    link = url_for("download", token=token, _external=True)
    return jsonify(ok=True, link=link)

@app.route("/d/<token>", methods=["GET","POST"])
def download(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row:
        abort(404)

    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        try: s3.delete_object(Bucket=S3_BUCKET, Key=row["stored_path"])
        except: pass
        c = db(); c.execute("DELETE FROM files WHERE token=?", (token,)); c.commit(); c.close()
        abort(410)

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

    share_link = url_for("download", token=token, _external=True)
    size_h = human(int(row["size_bytes"]))
    dt = datetime.fromisoformat(row["expires_at"]).replace(second=0, microsecond=0)
    expires_h = dt.strftime("%d-%m-%Y %H:%M")

    return render_template_string(
        DOWNLOAD_HTML,
        name=row["original_name"],
        size_human=size_h,
        expires_human=expires_h,
        token=token,
        share_link=share_link,
        base_css=BASE_CSS
    )

@app.route("/dl/<token>")
def download_file(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row:
        abort(404)
    url = s3.generate_presigned_url(
        "get_object", Params={"Bucket": S3_BUCKET, "Key": row["stored_path"]}, ExpiresIn=3600
    )
    return redirect(url)

# -------- Contact / aanvraag --------
EMAIL_RE  = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE  = re.compile(r"^[0-9+()\s-]{8,20}$")
ALLOWED_TB = {0.5, 1.0, 2.0, 5.0}

@app.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "GET":
        return render_template_string(
            CONTACT_HTML,
            error=None,
            form={"login_email":"", "storage_tb":"1", "company":"", "phone":""},
            base_css=BASE_CSS
        )

    login_email = (request.form.get("login_email") or "").strip()
    storage_tb_raw = (request.form.get("storage_tb") or "").strip()
    company = (request.form.get("company") or "").strip()
    phone = (request.form.get("phone") or "").strip()

    errors = []
    if not EMAIL_RE.match(login_email): errors.append("Vul een geldig e-mailadres in.")
    try: storage_tb = float(storage_tb_raw.replace(",", "."))
    except Exception: storage_tb = None
    if storage_tb not in ALLOWED_TB: errors.append("Kies een geldige opslaggrootte.")
    if len(company) < 2 or len(company) > 100: errors.append("Vul een geldige bedrijfsnaam in (min. 2 tekens).")
    if not PHONE_RE.match(phone): errors.append("Vul een geldig telefoonnummer in (8–20 tekens).")

    if errors:
        return render_template_string(
            CONTACT_HTML, error=" ".join(errors),
            form={"login_email":login_email,"storage_tb":(storage_tb_raw or "1"),"company":company,"phone":phone},
            base_css=BASE_CSS
        )

    subject = "Nieuwe aanvraag transfer-oplossing"
    price_map = {0.5:"€6/maand", 1.0:"€12/maand", 2.0:"€24/maand", 5.0:"€60/maand"}
    price   = price_map.get(storage_tb, "op aanvraag")
    body = (
        "Er is een nieuwe aanvraag binnengekomen:\n\n"
        f"- Gewenste inlog-e-mail: {login_email}\n"
        f"- Gewenste opslag: {storage_tb} TB (indicatie {price})\n"
        f"- Bedrijfsnaam: {company}\n"
        f"- Telefoonnummer: {phone}\n"
    )

    try:
        if smtp_configured():
            send_email(MAIL_TO, subject, body)
            return render_template_string(CONTACT_DONE_HTML, base_css=BASE_CSS)
    except Exception:
        pass

    from urllib.parse import quote
    mailto = f"mailto:{MAIL_TO}?subject={quote(subject)}&body={quote(body)}"
    return render_template_string(CONTACT_MAIL_FALLBACK_HTML, mailto_link=mailto, base_css=BASE_CSS)

# eenvoudige S3 healthcheck
@app.route("/health-s3")
def health():
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        return {"ok": True, "bucket": S3_BUCKET}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

if __name__ == "__main__":
    app.run(debug=True)
