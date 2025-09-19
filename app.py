#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, re, sqlite3, uuid, smtplib
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from flask import (
    Flask, request, redirect, url_for, abort, render_template_string, session, flash
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# --- Backblaze B2 / S3 ---
import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

# ---------------------- App-config ----------------------
BASE_DIR = Path(__file__).parent.resolve()
DB_PATH = BASE_DIR / "files.db"

# Uploadlimiet voor directe browser → B2 (geen streaming via server nodig)
MAX_CONTENT_LENGTH = 5 * 1024 * 1024 * 1024  # 5 GB

# Eenvoudige login
AUTH_EMAIL = "info@oldehanter.nl"
AUTH_PASSWORD = "Hulsmaat"

# Richtprijs (alleen UI, niet in e-mail)
CUSTOMER_PRICE_PER_TB_EUR = 12.0

# SMTP (Brevo/Gmail/Outlook) via environment
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
SMTP_USE_SSL = os.environ.get("SMTP_USE_SSL", "false").lower() == "true"
SMTP_FROM = os.environ.get("SMTP_FROM")
MAIL_TO = os.environ.get("MAIL_TO", "Patrick@oldehanter.nl")

# Backblaze B2 (S3-compatibel) via environment
S3_BUCKET = os.environ.get("S3_BUCKET")             # bv. MiniTransfer
S3_REGION = os.environ.get("S3_REGION", "eu-central-003")
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL") # bv. https://s3.eu-central-003.backblazeb2.com

# Boto3 client
s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
    config=BotoConfig(s3={"addressing_style": "virtual"})
)

app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", os.urandom(16)),
    MAX_CONTENT_LENGTH=MAX_CONTENT_LENGTH,
    SESSION_COOKIE_SAMESITE="Lax",
)

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
            stored_path TEXT NOT NULL,      -- B2 object key
            original_name TEXT NOT NULL,
            password_hash TEXT,
            expires_at TEXT NOT NULL,       -- ISO8601 in UTC
            size_bytes INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    con.commit(); con.close()

init_db()

# ---------------------- Utils ----------------------
def require_login() -> bool:
    return bool(session.get("authed"))

def is_smtp_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

def safe_days(value: str, default_days: float = 24.0) -> timedelta:
    try:
        d = float(value)
        if d <= 0: d = default_days
    except Exception:
        d = default_days
    return timedelta(days=d)

def human_size(n: int) -> str:
    for unit in ["B","KB","MB","GB","TB"]:
        if n < 1024.0:
            return (f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} {unit}")
        n /= 1024.0
    return f"{n:.1f} PB"

def send_email(to_addr: str, subject: str, body: str):
    if not is_smtp_configured():
        raise RuntimeError("SMTP niet geconfigureerd")
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
            if SMTP_USE_TLS: s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)

# ---------------------- UI templates ----------------------
LOGIN_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen - Olde Hanter Transfer</title>
<style>
*,*:before,*:after{box-sizing:border-box}
:root{--bg1:#60a5fa;--bg2:#a78bfa;--bg3:#34d399;--panel:rgba(255,255,255,.8);--panel-b:rgba(255,255,255,.45);--brand:#003366;--text:#0f172a}
html,body{height:100%} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);margin:0;position:relative;overflow-x:hidden}
.bg{position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vmax 60vmax at 15% 25%,var(--bg1) 0%,transparent 60%),
  radial-gradient(55vmax 55vmax at 85% 20%,var(--bg2) 0%,transparent 60%),
  radial-gradient(60vmax 60vmax at 50% 90%,var(--bg3) 0%,transparent 60%),
  linear-gradient(180deg,#eef2f7 0%,#e9eef6 100%);}
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
.wrap{max-width:520px;margin:6vh auto;padding:0 1rem}
.card{padding:2rem;background:var(--panel);border:1px solid var(--panel-b);border-radius:18px;box-shadow:0 18px 40px rgba(0,0,0,.12);backdrop-filter: blur(10px)}
h1{margin:0 0 1rem;color:var(--brand)}
label{display:block;margin:.6rem 0 .25rem;font-weight:600}
input{width:100%;padding:.9rem 1rem;border-radius:12px;border:1px solid #d1d5db;background:#fff}
button{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:12px;background:var(--brand);color:#fff;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(0,51,102,.25)}
.flash{background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem}
.footer{color:#334155;margin-top:1rem;text-align:center}
</style></head><body>
<div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card">
  <h1>Inloggen</h1>
  {% if error %}<div class="flash">{{ error }}</div>{% endif %}
  <form method="post" autocomplete="on">
    <label for="email">E-mail</label>
    <input id="email" name="email" type="email" placeholder="info@oldehanter.nl" value="info@oldehanter.nl" autocomplete="username" required>
    <label for="pw">Wachtwoord</label>
    <input id="pw" name="password" type="password" placeholder="Wachtwoord" required>
    <button type="submit">Inloggen</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

INDEX_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Olde Hanter Transfer</title>
<style>
*,*:before,*:after{box-sizing:border-box}
:root{--bg1:#60a5fa;--bg2:#a78bfa;--bg3:#34d399;--panel:rgba(255,255,255,.8);--panel-b:rgba(255,255,255,.45);--brand:#003366;--text:#0f172a}
html,body{height:100%} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);margin:0;position:relative;overflow-x:hidden}
.bg{position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vmax 60vmax at 15% 25%,var(--bg1) 0%,transparent 60%),
  radial-gradient(55vmax 55vmax at 85% 20%,var(--bg2) 0%,transparent 60%),
  radial-gradient(60vmax 60vmax at 50% 90%,var(--bg3) 0%,transparent 60%),
  linear-gradient(180deg,#eef2f7 0%,#e9eef6 100%);}
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
.card{padding:1.5rem;background:var(--panel);border:1px solid var(--panel-b);border-radius:18px;box-shadow:0 18px 40px rgba(0,0,0,.12);backdrop-filter: blur(10px)}
h1{margin:.25rem 0 1rem;color:var(--brand);font-size:2rem}
label{display:block;margin:.55rem 0 .25rem;font-weight:600}
input[type=file],input[type=text],input[type=password],input[type=number]{width:100%;padding:.9rem 1rem;border-radius:12px;border:1px solid #d1d5db;background:#fff}
.btn{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:12px;background:var(--brand);color:#fff;font-weight:700;cursor:pointer;box-shadow:0 4px 14px rgba(0,51,102,.25)}
.note{font-size:.95rem;color:#334155;margin-top:.5rem}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
.logout a{color:var(--brand);text-decoration:none;font-weight:700}
.flash{background:#dcfce7;color:#166534;padding:.6rem .9rem;border-radius:10px;display:inline-block}
.copy-btn{margin-left:.5rem;padding:.55rem .8rem;font-size:.9rem;background:#2563eb;border:none;border-radius:10px;color:#fff;cursor:pointer}
.footer{color:#334155;margin-top:1rem;text-align:center}
</style></head><body>
<div class="bg" aria-hidden="true"></div>
<div class="wrap">
  <div class="topbar">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div class="logout">Ingelogd als {{ user_email }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  {% with messages = get_flashed_messages() %}
    {% if messages %}<div class="flash">{{ messages[0] }}</div>{% endif %}
  {% endwith %}

  <form id="uploadForm" class="card" autocomplete="off">
    <label for="file">Bestand</label>
    <input id="file" type="file" name="file" required>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.6rem">
      <div>
        <label for="expiry">Verloopt over (dagen)</label>
        <input id="expiry" type="number" name="expiry_days" step="1" min="1" placeholder="24">
      </div>
      <div>
        <label for="pw">Wachtwoord (optioneel)</label>
        <input id="pw" type="password" name="password" placeholder="Laat leeg voor geen wachtwoord" autocomplete="new-password" autocapitalize="off" spellcheck="false">
      </div>
    </div>

    <button class="btn" type="submit">Uploaden</button>
    <p class="note">Max {{ max_mb }} MB per upload.</p>
  </form>

  {% if link %}
  <div class="card" style="margin-top:1rem">
    <strong>Deelbare link</strong>
    <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
      <input type="text" id="shareLink" value="{{ link }}" readonly>
      <button class="copy-btn" onclick="copyLink()">Kopieer</button>
    </div>
  </div>
  {% endif %}

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  function copyLink(){
    const el = document.getElementById('shareLink');
    if(!el) return;
    (navigator.clipboard?.writeText(el.value)||Promise.reject())
      .then(()=>alert('Link gekopieerd'))
      .catch(()=>{ el.select(); document.execCommand('copy'); alert('Link gekopieerd'); });
  }

  const form = document.getElementById('uploadForm');
  const fileInput = document.getElementById('file');
  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const file = fileInput.files[0];
    if(!file){ alert("Kies een bestand"); return; }
    const expiryDays = document.getElementById('expiry')?.value || '24';
    const password = document.getElementById('pw')?.value || '';

    // 1) presigned POST opvragen
    const signRes = await fetch("{{ url_for('sign_upload') }}", {
      method: "POST",
      headers: {"Content-Type":"application/x-www-form-urlencoded"},
      body: new URLSearchParams({ filename: file.name, size: file.size })
    });
    if(!signRes.ok){ alert("Kon upload niet voorbereiden"); return; }
    const sign = await signRes.json();

    // 2) uploaden naar B2 (S3) via form POST
    const fd = new FormData();
    Object.entries(sign.fields).forEach(([k,v])=>fd.append(k,v));
    fd.append("key", sign.key);
    fd.append("file", file);
    const s3Res = await fetch(sign.url, { method:"POST", body: fd });
    if(!s3Res.ok){ alert("Upload naar opslag mislukt"); return; }

    // 3) finalize → metadata opslaan + link tonen
    const finRes = await fetch("{{ url_for('finalize') }}", {
      method: "POST",
      headers: {"Content-Type":"application/x-www-form-urlencoded"},
      body: new URLSearchParams({ token: sign.token, key: sign.key, name: file.name, expiry_days: expiryDays, password: password })
    });
    const fin = await finRes.json();
    if(fin.ok){
      const box = document.createElement('div');
      box.className = 'card'; box.style.marginTop='1rem';
      box.innerHTML = `<strong>Deelbare link</strong>
        <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
          <input type="text" id="shareLink" value="${fin.link}" readonly>
          <button class="copy-btn" onclick="(navigator.clipboard?.writeText('${fin.link}')||Promise.reject()).then(()=>alert('Link gekopieerd')).catch(()=>{ const el=document.getElementById('shareLink'); el.select(); document.execCommand('copy'); alert('Link gekopieerd'); })">Kopieer</button>
        </div>`;
      document.querySelector('.wrap').appendChild(box);
    }else{
      alert('Opslaan mislukt');
    }
  });
</script>
</body></html>
"""

DOWNLOAD_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Download bestand - Olde Hanter</title>
<style>
*,*:before,*:after{box-sizing:border-box}
:root{--bg1:#0ea5e9;--bg2:#6366f1;--bg3:#22c55e;--panel:rgba(255,255,255,.75);--panel-b:rgba(255,255,255,.35)}
html,body{height:100%} body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#111827;margin:0;position:relative;overflow-x:hidden}
.bg{position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vmax 60vmax at 20% 20%,var(--bg1) 0%,transparent 60%),
  radial-gradient(50vmax 50vmax at 80% 30%,var(--bg2) 0%,transparent 60%),
  radial-gradient(55vmax 55vmax at 40% 80%,var(--bg3) 0%,transparent 60%),
  linear-gradient(180deg,#eef2f7 0%,#eaeef5 100%)}
.bg:before{content:"";position:absolute;inset:-10%;background:
  radial-gradient(40% 60% at 20% 30%,rgba(255,255,255,.35),transparent),
  radial-gradient(50% 70% at 80% 20%,rgba(255,255,255,.25),transparent);
  animation:f1 16s linear infinite;will-change:transform}
.bg:after{content:"";position:absolute;inset:-10%;background:
  radial-gradient(35% 50% at 60% 70%,rgba(255,255,255,.25),transparent),
  radial-gradient(45% 55% at 30% 80%,rgba(255,255,255,.2),transparent);
  animation:f2 24s linear infinite;will-change:transform}
@keyframes f1{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(1.5%,-1.5%,0) rotate(180deg)}100%{transform:translate3d(0,0,0) rotate(360deg)}}
@keyframes f2{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(-1.25%,1.25%,0) rotate(-180deg)}100%{transform:translate3d(0,0,0) rotate(-360deg)}}
@media (prefers-reduced-motion:reduce){.bg:before,.bg:after{animation:none}}
.wrap{max-width:820px;margin:3.5rem auto;padding:0 1rem}
.panel{background:var(--panel);border:1px solid var(--panel-b);border-radius:16px;box-shadow:0 12px 30px rgba(0,0,0,.08);padding:1.5rem;backdrop-filter: blur(10px)}
.meta{margin:.5rem 0 1rem;color:#374151}
.btn{display:inline-block;padding:.95rem 1.25rem;background:#003366;color:#fff;border-radius:10px;text-decoration:none;font-weight:700}
.linkbox{margin-top:1rem;background:rgba(255,255,255,.65);border:1px solid rgba(255,255,255,.35);border-radius:10px;padding:.75rem}
input[type=text]{width:100%;padding:.8rem .9rem;border-radius:10px;border:1px solid #d1d5db;background:#fff}
.copy-btn{margin-left:.5rem;padding:.55rem .8rem;font-size:.85rem;background:#2563eb;border:none;border-radius:8px;color:#fff;cursor:pointer}
.footer{color:#334155;margin-top:1rem;text-align:center}
</style></head><body>
<div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="panel">
  <h1>Download bestand</h1>
  <div class="meta">
    <div><strong>Bestandsnaam:</strong> {{ name }}</div>
    <div><strong>Grootte:</strong> {{ size_human }}</div>
    <div><strong>Verloopt:</strong> {{ expires_human }}</div>
  </div>
  <a class="btn" href="{{ url_for('download_file', token=token) }}">Download</a>
  <div class="linkbox" style="margin-top:1rem">
    <div><strong>Deelbare link</strong></div>
    <div style="display:flex;gap:.5rem;align-items:center;">
      <input type="text" id="shareLink" value="{{ share_link }}" readonly>
      <button class="copy-btn" onclick="(navigator.clipboard?.writeText('{{ share_link }}')||Promise.reject()).then(()=>alert('Link gekopieerd')).catch(()=>{ const el=document.getElementById('shareLink'); el.select(); document.execCommand('copy'); alert('Link gekopieerd'); })">Kopieer</button>
    </div>
  </div>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

CONTACT_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eigen transfer-oplossing - Olde Hanter</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;margin:0}
.wrap{max-width:720px;margin:3rem auto;padding:0 1rem}
.card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}
h1{margin:.25rem 0 1rem;color:#003366}
label{display:block;margin:.55rem 0 .25rem;font-weight:600}
input,select,button{width:100%;padding:.85rem .95rem;border-radius:10px;border:1px solid #d1d5db;background:#fff}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
@media (max-width:720px){.grid{grid-template-columns:1fr}}
.btn{margin-top:1rem;padding:.95rem 1.2rem;border:0;border-radius:10px;background:#003366;color:#fff;font-weight:700;cursor:pointer}
.note{font-size:.95rem;color:#6b7280;margin-top:.5rem}
.error{background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem}
.footer{color:#6b7280;margin-top:1rem;text-align:center}
</style></head><body>
<div class="wrap"><div class="card">
  <h1>Eigen transfer-oplossing aanvragen</h1>
  {% if error %}<div class="error">{{ error }}</div>{% endif %}
  <form id="contactForm" method="post" action="{{ url_for('contact') }}" novalidate>
    <div class="grid">
      <div>
        <label for="login_email">Gewenste inlog-e-mail</label>
        <input id="login_email" name="login_email" type="email" placeholder="naam@bedrijf.nl" value="{{ form.login_email or '' }}" required>
      </div>
      <div>
        <label for="storage_tb">Gewenste opslaggrootte</label>
        <select id="storage_tb" name="storage_tb" required>
          <option value="">Maak een keuze…</option>
          <option value="0.5" {{ 'selected' if form.storage_tb=='0.5' else '' }}>0,5 TB</option>
          <option value="1"   {{ 'selected' if (form.storage_tb or '1')=='1' else '' }}>1 TB</option>
          <option value="2"   {{ 'selected' if form.storage_tb=='2' else '' }}>2 TB</option>
          <option value="5"   {{ 'selected' if form.storage_tb=='5' else '' }}>5 TB</option>
        </select>
      </div>
    </div>
    <div class="grid" style="margin-top:1rem">
      <div>
        <label for="company">Bedrijfsnaam</label>
        <input id="company" name="company" type="text" placeholder="Bedrijfsnaam BV" value="{{ form.company or '' }}" minlength="2" maxlength="100" required>
      </div>
      <div>
        <label for="phone">Telefoonnummer</label>
        <input id="phone" name="phone" type="tel" placeholder="+31 6 12345678" value="{{ form.phone or '' }}" pattern="^[0-9+()\\s-]{8,20}$" required>
      </div>
    </div>
    <p class="note">Richtprijs: €<span id="cost_offer">{{ offer_cost_eur }}</span>/maand (op basis van <span id="tb_val">{{ form.storage_tb or '1' }}</span> TB).</p>
    <button class="btn" id="submitBtn" type="submit">Verstuur aanvraag</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
<script>
  let baseOffer = {{ base_offer }};
  const storageSel = document.getElementById('storage_tb');
  const offerEl = document.getElementById('cost_offer');
  const tbVal = document.getElementById('tb_val');
  function updatePrice(){
    const tb = parseFloat(storageSel.value || "1");
    const offer = tb * baseOffer;
    offerEl.textContent = offer.toFixed(2).replace('.', ',');
    tbVal.textContent = (storageSel.value || "1").toString().replace('.', ',');
  }
  storageSel.addEventListener('change', updatePrice);
  updatePrice();
</script>
</body></html>
"""

CONTACT_DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag verstuurd</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;margin:0}.wrap{max-width:680px;margin:3rem auto;padding:0 1rem}.card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}.footer{color:#6b7280;margin-top:1rem;text-align:center}</style>
</head><body><div class="wrap"><div class="card"><h1>Aanvraag is verstuurd</h1><p>Bedankt! We nemen zo snel mogelijk contact met je op.</p><p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div></body></html>
"""

CONTACT_MAIL_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title>
<style>body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f2f5f9;margin:0}.wrap{max-width:680px;margin:3rem auto;padding:0 1rem}.card{padding:1.25rem;background:#fff;border:1px solid #e5e7eb;border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06)}.btn{display:inline-block;margin-top:1rem;padding:.95rem 1.2rem;border-radius:10px;background:#003366;color:#fff;text-decoration:none;font-weight:700}.footer{color:#6b7280;margin-top:1rem;text-align:center}</style>
</head><body><div class="wrap"><div class="card"><h1>Aanvraag gereed</h1><p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p><a class="btn" href="{{ mailto_link }}">Open e-mail</a><p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div></body></html>
"""

# ---------------------- Routes ----------------------
@app.route("/", methods=["GET"])
def home():
    if not require_login(): return redirect(url_for("login"))
    return render_template_string(
        INDEX_HTML,
        link=None,
        max_mb=app.config["MAX_CONTENT_LENGTH"] // (1024*1024),
        user_email=session.get("user_email", AUTH_EMAIL),
    )

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip()
        pw = request.form.get("password") or ""
        if email.lower() == AUTH_EMAIL.lower() and pw == AUTH_PASSWORD:
            session["authed"] = True
            session["user_email"] = email
            return redirect(url_for("home"))
        return render_template_string(LOGIN_HTML, error="Onjuiste inloggegevens.")
    return render_template_string(LOGIN_HTML, error=None)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ---------- Direct-to-B2 flow ----------
@app.route("/sign-upload", methods=["POST"])
def sign_upload():
    if not require_login(): return abort(401)
    filename = secure_filename(request.form.get("filename") or "upload.bin")
    size = int(request.form.get("size") or 0)
    if size > MAX_CONTENT_LENGTH:
        return {"error": "File too large"}, 400

    token = uuid.uuid4().hex[:10]
    object_key = f"uploads/{token}__{filename}"

    conditions = [
        {"bucket": S3_BUCKET},
        ["starts-with", "$key", "uploads/"],
        ["content-length-range", 0, MAX_CONTENT_LENGTH],
    ]
    fields = {"acl": "private"}
    post = s3.generate_presigned_post(
        Bucket=S3_BUCKET,
        Key=object_key,
        Fields=fields,
        Conditions=conditions,
        ExpiresIn=900,  # 15 min
    )
    return {"token": token, "key": object_key, "url": post["url"], "fields": post["fields"]}

@app.route("/finalize", methods=["POST"])
def finalize():
    if not require_login(): return abort(401)
    token = request.form.get("token")
    object_key = request.form.get("key")
    original_name = request.form.get("name")
    expiry_days = request.form.get("expiry_days", "24")
    pw = request.form.get("password") or ""

    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=object_key)
        size_bytes = int(head["ContentLength"])
    except ClientError:
        return {"ok": False, "error": "Object niet gevonden"}, 400

    expires_at = (datetime.now(timezone.utc) + safe_days(expiry_days)).isoformat()
    pw_hash = generate_password_hash(pw) if pw else None

    con = get_db()
    con.execute(
        "INSERT INTO files(token, stored_path, original_name, password_hash, expires_at, size_bytes, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (token, object_key, original_name, pw_hash, expires_at, size_bytes, datetime.now(timezone.utc).isoformat()),
    )
    con.commit(); con.close()

    link = url_for("download", token=token, _external=True)
    return {"ok": True, "link": link}

@app.route("/d/<token>", methods=["GET","POST"])
def download(token: str):
    con = get_db()
    row = con.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone()
    con.close()
    if not row: abort(404)

    # verlopen? → opruimen
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        try: s3.delete_object(Bucket=S3_BUCKET, Key=row["stored_path"])
        except Exception: pass
        con = get_db(); con.execute("DELETE FROM files WHERE token=?", (token,)); con.commit(); con.close()
        abort(410)

    # wachtwoordcontrole
    if row["password_hash"]:
        if request.method == "GET":
            return """
            <form method="post" style="max-width:420px;margin:3rem auto;font-family:system-ui">
              <h3>Voer wachtwoord in</h3>
              <input type="password" name="password" style="width:100%;padding:.7rem;border:1px solid #ccc;border-radius:8px" required />
              <button type="submit" style="margin-top:.6rem;padding:.6rem 1rem;border:0;border-radius:8px;background:#003366;color:#fff">Ontgrendel</button>
            </form>"""
        pw = request.form.get("password", "")
        if not check_password_hash(row["password_hash"], pw):
            return """
            <form method="post" style="max-width:420px;margin:3rem auto;font-family:system-ui">
              <h3 style="color:#b91c1c">Onjuist wachtwoord</h3>
              <input type="password" name="password" style="width:100%;padding:.7rem;border:1px solid #ccc;border-radius:8px" required />
              <button type="submit" style="margin-top:.6rem;padding:.6rem 1rem;border:0;border-radius:8px;background:#003366;color:#fff">Opnieuw</button>
            </form>"""

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
    if not row: abort(404)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": S3_BUCKET, "Key": row["stored_path"]},
        ExpiresIn=60 * 60  # 1 uur
    )
    return redirect(url)

# ---------- Contact ----------
EMAIL_RE  = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE  = re.compile(r"^[0-9+()\s-]{8,20}$")
ALLOWED_TB = {0.5, 1.0, 2.0, 5.0}

@app.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "GET":
        return render_template_string(
            CONTACT_HTML,
            base_offer=CUSTOMER_PRICE_PER_TB_EUR,
            offer_cost_eur=f"{(1.0 * CUSTOMER_PRICE_PER_TB_EUR):.2f}".replace('.', ','),
            error=None,
            form={"login_email":"", "storage_tb":"1", "company":"", "phone":""},
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
    if not PHONE_RE.match(phone): errors.append("Vul een geldig telefoonnummer in (8–20 cijfers/tekens).")

    if errors:
        return render_template_string(
            CONTACT_HTML,
            base_offer=CUSTOMER_PRICE_PER_TB_EUR,
            offer_cost_eur=f"{((storage_tb or 1.0) * CUSTOMER_PRICE_PER_TB_EUR):.2f}".replace('.', ','),
            error=" ".join(errors),
            form={"login_email":login_email,"storage_tb":(storage_tb_raw or "1"),"company":company,"phone":phone},
        )

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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
