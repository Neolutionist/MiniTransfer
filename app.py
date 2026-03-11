#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========= MiniTransfer – Olde Hanter (met abonnementbeheer + PayPal webhook) =========
# - Login met vast wachtwoord "Hulsmaat" (e-mail vooraf ingevuld)
# - Upload (files/folders) naar B2 (S3) met voortgang
# - Downloadpagina met zip-stream en precheck
# - Contact/aanvraag met PayPal abonnement-knop (pas zichtbaar bij volledig geldig formulier)
# - Abonnementbeheer: opslaan subscriptionID, opzeggen, plan wijzigen (revise)
# - Webhook: verifieert PayPal-events en mailt bij activatie/annulering/suspense/reactivatie en bij elke capture
# - Domeinen: ondersteunt minitransfer.onrender.com én downloadlink.nl in get_base_host()
# ======================================================================================

import os, re, uuid, smtplib, sqlite3, logging, base64, json, urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, abort, render_template_string,
    session, jsonify, Response, stream_with_context, g
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError
from zipstream import ZipStream  # zipstream-ng

# --- Internal cleanup endpoint (cron -> webservice) ---
from cleanup_expired import cleanup_expired, resolve_data_dir

# ---------------- Config ----------------
BASE_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("DATA_DIR", "/var/data"))
DB_PATH  = DATA_DIR / "files_multi.db"
DATA_DIR.mkdir(parents=True, exist_ok=True)

AUTH_EMAIL = os.environ.get("AUTH_EMAIL", "info@oldehanter.nl")
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "Hulsmaat")  # vast wachtwoord voor het inloggen

S3_BUCKET = os.getenv("S3_BUCKET")
S3_REGION = os.getenv("S3_REGION", "eu-central-003")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")

if not S3_BUCKET or not S3_ENDPOINT_URL:
    raise RuntimeError(
        "❌ S3-configuratie mist! Controleer of 'S3_BUCKET' en 'S3_ENDPOINT_URL' zijn ingesteld in Render → Environment."
    )


SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
MAIL_TO   = os.environ.get("MAIL_TO", "Patrick@oldehanter.nl")

# PayPal Subscriptions
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE      = os.environ.get("PAYPAL_API_BASE", "https://api-m.paypal.com")  # sandbox: https://api-m.sandbox.paypal.com

PAYPAL_PLAN_0_5  = os.environ.get("PAYPAL_PLAN_0_5", "P-9SU96133E7732223VNDIEDIY")  # 0,5 TB – €12/mnd
PAYPAL_PLAN_1    = os.environ.get("PAYPAL_PLAN_1",   "P-0E494063742081356NDIEDUI")  # 1 TB   – €15/mnd
PAYPAL_PLAN_2    = os.environ.get("PAYPAL_PLAN_2",   "P-8TG57271W98348431NDIEECA")  # 2 TB   – €20/mnd
PAYPAL_PLAN_5    = os.environ.get("PAYPAL_PLAN_5",   "P-78R23653MC041353LNDIEEOQ")  # 5 TB   – €30/mnd

PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID")  # vanuit Developer Dashboard → My Apps & Credentials → jouw app → Webhooks

PLAN_MAP = {
    "0.5": PAYPAL_PLAN_0_5,
    "1":   PAYPAL_PLAN_1,
    "2":   PAYPAL_PLAN_2,
    "5":   PAYPAL_PLAN_5,
}
REVERSE_PLAN_MAP = {v: k for k, v in PLAN_MAP.items() if v}

s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
    config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "olde-hanter-simple-secret")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", str(1024 * 1024 * 1024 * 20))),
)

TOKEN_RE = re.compile(r"^[a-f0-9]{10}$")
MIN_EXPIRY_DAYS = float(os.environ.get("MIN_EXPIRY_DAYS", "0.04"))  # ~1 uur
MAX_EXPIRY_DAYS = float(os.environ.get("MAX_EXPIRY_DAYS", "365"))
MAX_TITLE_LENGTH = int(os.environ.get("MAX_TITLE_LENGTH", "120"))

# --- Render healthcheck fix ---
HEALTH_PATHS = ("/health", "/health-s3", "/__health")

@app.before_request
def allow_health():
    """Laat Render healthchecks (en vergelijkbare) gewoon 200 OK teruggeven."""
    if request.path.startswith(HEALTH_PATHS):
        return  # Geen redirect of blokkade; laat de route doorgaan

# ---- Multi-tenant configuratie (HOST -> tenant) ----
TENANTS = {
    "oldehanter.downloadlink.nl": {
        "slug": "oldehanter",
        "mail_to": os.environ.get("MAIL_TO", "Patrick@oldehanter.nl"),
    }
}

def current_tenant():
    host = (request.headers.get("Host") or "").lower()
    return TENANTS.get(host) or TENANTS["oldehanter.downloadlink.nl"]
# ----------------------------------------------------

# --- Redirect config toevoegen ---
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config.update(PREFERRED_URL_SCHEME="https", SESSION_COOKIE_SECURE=True)

import os
CANONICAL_HOST = os.environ.get("CANONICAL_HOST", "oldehanter.downloadlink.nl").lower()
OLD_HOST = os.environ.get("OLD_HOST", "minitransfer.onrender.com").lower()

@app.before_request
def _redirect_old_host():
    host = (request.headers.get("Host") or "").lower()
    if host == OLD_HOST:
        new_url = request.url.replace(f"//{OLD_HOST}", f"//{CANONICAL_HOST}", 1)
        return redirect(new_url, code=308)
# --- Einde redirect config ---

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# --------------- DB --------------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA journal_mode = WAL")
    c.execute("PRAGMA busy_timeout = 5000")
    return c

def init_db():
    c = db()
    c.execute("""
      CREATE TABLE IF NOT EXISTS packages (
        token TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL,
        password_hash TEXT,
        created_at TEXT NOT NULL,
        title TEXT
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        s3_key TEXT NOT NULL,
        name TEXT NOT NULL,
        path TEXT NOT NULL,
        size_bytes INTEGER NOT NULL
      )
    """)
    c.execute("""
      CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        login_email TEXT NOT NULL,
        plan_value TEXT NOT NULL,
        subscription_id TEXT UNIQUE NOT NULL,
        status TEXT DEFAULT 'ACTIVE',
        created_at TEXT NOT NULL
      )
    """)
    c.commit(); c.close()
init_db()

def _col_exists(conn, table, col):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def migrate_add_tenant_columns():
    conn = db()
    try:
        # packages
        if not _col_exists(conn, "packages", "tenant_id"):
            conn.execute("ALTER TABLE packages ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE packages SET tenant_id = 'oldehanter' WHERE tenant_id IS NULL")
        # items
        if not _col_exists(conn, "items", "tenant_id"):
            conn.execute("ALTER TABLE items ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE items SET tenant_id = 'oldehanter' WHERE tenant_id IS NULL")
        # subscriptions
        if not _col_exists(conn, "subscriptions", "tenant_id"):
            conn.execute("ALTER TABLE subscriptions ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE subscriptions SET tenant_id = 'oldehanter' WHERE tenant_id IS NULL")

        conn.commit()
    finally:
        conn.close()

migrate_add_tenant_columns()

# -------------- CSS --------------
BASE_CSS = """
*,*:before,*:after{box-sizing:border-box}
:root{
  /* Kleuren */
  --c1:#86b6ff; --c2:#b59cff; --c3:#5ce1b9; --c4:#ffe08a; --c5:#ffa2c0;
  --brand:#0f4c98; --brand-2:#003366;
  --text:#0f172a; --muted:#475569; --line:#d1d5db; --ring:#2563eb;
  --surface:#ffffff; --surface-2:#f1f5f9;
  --panel:rgba(255,255,255,.82); --panel-b:rgba(255,255,255,.45);
  /* Animatie snelheden */
  --t-slow: 28s;
  --t-med:  18s;
  --t-fast:  8s;
}
/* Dark mode (volgt OS) */
@media (prefers-color-scheme: dark){
  :root{
    --brand:#7db4ff; --brand-2:#4a7fff;
    --text:#e5e7eb; --muted:#9aa3b2; --line:#3b4252; --ring:#8ab4ff;
    --surface:#0b1020; --surface-2:#0f172a;
    --panel:rgba(13,20,40,.72); --panel-b:rgba(13,20,40,.4);
  }
}

html,body{height:100%}
body{
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  color:var(--text); margin:0; position:relative; overflow-x:hidden;
  background: var(--surface);
}

/* ======= Nieuwe achtergrond ======= */
.bg{
  position:fixed; inset:0; z-index:-2; overflow:hidden;
  /* Basismix (zachte radialen + subtiele vertical fade) */
  background:
    radial-gradient(40vmax 40vmax at 14% 24%, var(--c1) 0%, transparent 60%),
    radial-gradient(38vmax 38vmax at 86% 30%, var(--c2) 0%, transparent 60%),
    radial-gradient(50vmax 50vmax at 52% 92%, var(--c3) 0%, transparent 60%),
    linear-gradient(180deg, #edf3ff 0%, #eef4fb 100%);
  filter:saturate(1.06);
  animation: hueShift var(--t-slow) linear infinite;
}

/* Aurora laag */
.bg::before,
.bg::after{
  content:""; position:absolute; inset:-10%;
  /* Aurora met conic-gradients; de mask maakt vloeiende vormen */
  background:
    conic-gradient(from 0deg at 30% 60%, rgba(255,255,255,.14), rgba(255,255,255,0) 60%),
    conic-gradient(from 180deg at 70% 40%, rgba(255,255,255,.10), rgba(255,255,255,0) 60%);
  mix-blend-mode: overlay;
  will-change: transform, opacity;
}
.bg::before{
  animation: driftA var(--t-med) ease-in-out infinite alternate;
  opacity:.85;
  -webkit-mask-image: radial-gradient(65% 55% at 35% 60%, #000 0 60%, transparent 62%);
          mask-image: radial-gradient(65% 55% at 35% 60%, #000 0 60%, transparent 62%);
}
.bg::after{
  animation: driftB var(--t-slow) ease-in-out infinite;
  opacity:.65;
  -webkit-mask-image: radial-gradient(75% 65% at 70% 40%, #000 0 60%, transparent 62%);
          mask-image: radial-gradient(75% 65% at 70% 40%, #000 0 60%, transparent 62%);
}

/* Subtiele korrel / film grain (zonder externe asset) */
.bg::marker{display:none}
.bg > i{display:none}
.bg::before, .bg::after { backdrop-filter: saturate(1.05) blur(2px); }
.bg + .grain{ /* aparte overlay via pseudo-element lukt niet overal; gebruik extra div niet nodig – we faken ruis met gradients */
  display:none;
}

/* Glass kaarten en UI */
.wrap{max-width:980px;margin:6vh auto;padding:0 1rem}
.card{
  padding:1.5rem; background:var(--panel); border:1px solid var(--panel-b);
  border-radius:18px; box-shadow:0 18px 40px rgba(0,0,0,.12);
  backdrop-filter: blur(10px) saturate(1.05);
}
h1{line-height:1.15}
.footer{color:#334155;margin-top:1.2rem;text-align:center}
.small{font-size:.9rem;color:var(--muted)}

/* Forms/Buttons */
label{display:block;margin:.65rem 0 .35rem;font-weight:600;color:var(--text)}
.input, input[type=text], input[type=password], input[type=email], input[type=number],
select, textarea{
  width:100%; display:block; appearance:none;
  padding:.85rem 1rem; border-radius:12px; border:1px solid var(--line);
  background:color-mix(in oklab, var(--surface-2) 90%, white 10%); color:var(--text);
  outline:none; transition: box-shadow .15s, border-color .15s, background .15s;
}
input:focus, .input:focus, select:focus, textarea:focus{
  border-color: var(--ring); box-shadow: 0 0 0 4px color-mix(in oklab, var(--ring) 30%, transparent);
}
input[type=file]{padding:.55rem 1rem; background:var(--surface-2); cursor:pointer}
input[type=file]::file-selector-button{
  margin-right:.75rem; border:1px solid var(--line);
  background:var(--surface); color:var(--text);
  padding:.55rem .9rem; border-radius:10px; cursor:pointer;
}
.btn{
  padding:.85rem 1.05rem;border:0;border-radius:12px;
  background:linear-gradient(180deg, var(--brand), color-mix(in oklab, var(--brand) 85%, black 15%));
  color:#fff;font-weight:700;cursor:pointer;
  box-shadow:0 4px 14px rgba(15,76,152,.25); transition:filter .15s, transform .02s;
  font-size:.95rem; line-height:1;
}
.btn.small{padding:.55rem .8rem;font-size:.9rem}
.btn:hover{filter:brightness(1.05)}
.btn:active{transform:translateY(1px)}
.btn.secondary{background:linear-gradient(180deg, var(--brand-2), color-mix(in oklab, var(--brand-2) 85%, black 15%))}

/* Progress */
.progress{
  height:14px;background:color-mix(in oklab, var(--surface-2) 85%, white 15%);
  border-radius:999px;overflow:hidden;margin-top:.75rem;border:1px solid #dbe5f4; position:relative;
}
.progress > i{
  display:block;height:100%;width:0%;
  background:linear-gradient(90deg,#0f4c98,#1e90ff);
  transition:width .12s ease; position:relative;
}
.progress > i::after{
  content:""; position:absolute; inset:0;
  background-image: linear-gradient(135deg, rgba(255,255,255,.28) 25%, transparent 25%, transparent 50%, rgba(255,255,255,.28) 50%, rgba(255,255,255,.28) 75%, transparent 75%, transparent);
  background-size:24px 24px; animation: stripes 1s linear infinite; mix-blend-mode: overlay;
}
.progress.indet > i{ width:40%; animation: indet-move 1.2s linear infinite; }

@keyframes indet-move{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}
@keyframes stripes{0%{transform:translateX(0)}100%{transform:translateX(24px)}}

/* Tabel */
.table{width:100%;border-collapse:collapse;margin-top:.6rem}
.table th,.table td{padding:.55rem .7rem;border-bottom:1px solid #e5e7eb;text-align:left}

/* Responsive tabel */
@media (max-width: 680px){
  .table thead{display:none}
  .table, .table tbody, .table tr, .table td{display:block;width:100%}
  .table tr{margin-bottom:.6rem;background:rgba(255,255,255,.55);border:1px solid #e5e7eb;border-radius:10px;padding:.4rem .6rem}
  .table td{border:0;padding:.25rem 0}
  .table td[data-label]:before{content:attr(data-label) ": ";font-weight:600;color:#334155}
  .cols-2{ grid-template-columns: 1fr !important; }
}

/* ZIP lijst kolombreedte */
.table th.col-size,
.table td.col-size,
.table td[data-label="Grootte"]{
  white-space:nowrap; text-align:right; min-width:72px;
}

/* Aurora animaties */
@keyframes driftA{
  0%{transform:translate3d(0,0,0) scale(1)}
  50%{transform:translate3d(.6%,1.4%,0) scale(1.03)}
  100%{transform:translate3d(0,0,0) scale(1)}
}
@keyframes driftB{
  0%{transform:rotate(0deg) translateY(0)}
  50%{transform:rotate(180deg) translateY(-1%)}
  100%{transform:rotate(360deg) translateY(0)}
}
/* Kleurverschuiving over tijd */
@keyframes hueShift{
  0%{filter:hue-rotate(0deg) saturate(1.06)}
  100%{filter:hue-rotate(360deg) saturate(1.06)}
}

/* Respecteer reduced motion */
@media (prefers-reduced-motion: reduce){
  .bg, .bg::before, .bg::after{ animation: none !important; }
}
"""

# --- Favicon (SVG) ---
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#1E3A8A"/>
  <text x="50%" y="55%" text-anchor="middle" dominant-baseline="middle"
        font-family="Segoe UI, Roboto, sans-serif" font-size="28" font-weight="700"
        fill="white">OH</text>
</svg>"""

from urllib.parse import quote as _q
FAVICON_DATA_URL = "data:image/svg+xml;utf8," + _q(FAVICON_SVG)

@app.route("/favicon.svg")
def favicon_svg():
    return Response(FAVICON_SVG, mimetype="image/svg+xml")

@app.route("/favicon.ico")
def favicon_ico():
    # browsers die /favicon.ico hardcoderen -> redirect naar svg
    return redirect(url_for("favicon_svg"), code=302)
    
# -------------- Templates --------------
BG_DIV = '<div class="bg" aria-hidden="true"></div>'
HTML_HEAD_ICON = f"""
<link rel="icon" href="{FAVICON_DATA_URL}" type="image/svg+xml"/>
<link rel="alternate icon" href="{{{{ url_for('favicon_svg') }}}}" type="image/svg+xml"/>
<link rel="shortcut icon" href="{{{{ url_for('favicon_ico') }}}}"/>
"""

LOGIN_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen – Olde Hanter</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card" style="max-width:460px;margin:auto">
  <h1 style="color:var(--brand)">Inloggen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}
<style>
/* Masker een tekstveld als een wachtwoordveld */
.input.pw-mask {
  -webkit-text-security: disc;    /* Chrome/Safari */
  text-security: disc;            /* sommige browsers */
}
</style>

<form method="post" autocomplete="off">
  <!-- honeypots tegen autofill -->
  <input type="text" name="x" style="display:none">
  <input type="password" name="y" style="display:none" autocomplete="new-password">

  <label for="email">E-mail</label>
  <input id="email" class="input" name="email" type="email"
         value="{{ auth_email }}" autocomplete="username" required>

  <label for="pw_ui">Wachtwoord</label>
  <!-- Zichtbaar veld is GEEN password-type -> geen generator/autofill -->
  <input id="pw_ui"
         class="input pw-mask"
         type="text"
         name="pw_ui"
         placeholder="Wachtwoord"
         autocomplete="off"
         autocapitalize="off"
         autocorrect="off"
         spellcheck="false"
         inputmode="text"
         data-lpignore="true"
         data-1p-ignore="true">

  <!-- Echt verborgen password-veld voor submit naar server -->
  <input id="pw_real" type="password" name="password" style="display:none" tabindex="-1" autocomplete="off">

  <button class="btn" type="submit" style="margin-top:1rem;width:100%">Inloggen</button>
</form>

<script>
(function(){
  const form   = document.currentScript.previousElementSibling;
  const pwUI   = document.getElementById('pw_ui');
  const pwReal = document.getElementById('pw_real');

  // extra defensie
  setTimeout(()=>{ try{ pwUI.value=''; }catch(e){} }, 0);

  form.addEventListener('submit', function(){
    pwReal.value = pwUI.value || '';
  }, {passive:true});
})();
</script>

  <p class="footer small">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

PASS_PROMPT_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Pakket beveiligd – Olde Hanter</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card" style="max-width:560px;margin:6vh auto">
  <h1>Beveiligd pakket</h1>
  <p class="small" style="margin-top:.2rem">Voer het wachtwoord in om dit pakket te openen.</p>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}
  <form method="post" autocomplete="off">
    <input type="text" name="a" style="display:none"><input type="password" name="b" style="display:none">
    <label for="pw">Wachtwoord</label>
    <input id="pw" class="input" type="password" name="password" placeholder="Wachtwoord"
           required autocomplete="new-password" autocapitalize="off" spellcheck="false">
    <button class="btn" style="margin-top:1rem">Ontgrendel</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

INDEX_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
  <title>Upload • Olde Hanter</title>
  {{ head_icon|safe }}
  <style>
    {{ base_css }}

    html, body { min-height:100%; }

    body{
      margin:0;
      overflow-x:hidden;
      position:relative;
      color:#fff;
      background:
        radial-gradient(circle at 12% 18%, rgba(255,0,153,.30), transparent 24%),
        radial-gradient(circle at 83% 24%, rgba(0,255,255,.24), transparent 28%),
        radial-gradient(circle at 50% 82%, rgba(255,255,0,.18), transparent 24%),
        linear-gradient(135deg, #0d0018 0%, #1c0033 18%, #001a3b 38%, #24002e 58%, #220014 76%, #090010 100%);
    }

    .psy-overlay,
    .psy-overlay::before,
    .psy-overlay::after{
      content:"";
      position:fixed;
      inset:-18%;
      pointer-events:none;
      z-index:0;
    }

    .psy-overlay{
      background:
        conic-gradient(from 0deg,
          rgba(255,0,153,.18),
          rgba(255,255,0,.14),
          rgba(0,255,255,.16),
          rgba(138,46,255,.18),
          rgba(255,94,0,.16),
          rgba(255,0,153,.18));
      filter: blur(56px) saturate(1.6);
      mix-blend-mode: screen;
      animation: spinGlow 22s linear infinite;
    }

    .psy-overlay::before{
      background:
        repeating-radial-gradient(
          circle at center,
          rgba(255,255,255,.05) 0 12px,
          rgba(255,255,255,0) 12px 28px
        );
      opacity:.30;
      animation: pulseRings 9s ease-in-out infinite;
    }

    .psy-overlay::after{
      background:
        linear-gradient(90deg,
          rgba(255,0,153,.10),
          rgba(0,255,255,.10),
          rgba(255,255,0,.10),
          rgba(138,46,255,.10),
          rgba(255,0,153,.10));
      background-size:300% 300%;
      mix-blend-mode:overlay;
      animation: driftColors 12s ease-in-out infinite;
    }

    .shell{
      position:relative;
      z-index:2;
      max-width:1180px;
      margin:4vh auto;
      padding:0 16px 28px;
    }

    .hdr{
      display:flex;
      align-items:center;
      justify-content:space-between;
      margin-bottom:16px;
      gap:14px;
      flex-wrap:wrap;
    }

    .brand{
      margin:0;
      font-weight:900;
      font-size:clamp(2rem,4vw,3rem);
      line-height:1.02;
      text-transform:uppercase;
      letter-spacing:.03em;
      color:#fff;
      text-shadow:
        0 0 10px #ff00a8,
        0 0 22px #ff00a8,
        0 0 34px #00f7ff,
        0 0 54px #8a2eff;
    }

    .hdr .right{
      display:flex;
      align-items:center;
      gap:10px;
      flex-wrap:wrap;
      justify-content:flex-end;
    }

    .who{
      color:rgba(255,255,255,.88);
      font-size:.95rem;
      padding:.7rem 1rem;
      border-radius:999px;
      background:rgba(255,255,255,.08);
      border:1px solid rgba(255,255,255,.14);
      box-shadow:0 0 16px rgba(255,255,255,.05);
      backdrop-filter: blur(10px);
    }
    .who a{ color:#ffe600; text-decoration:none; font-weight:700; }
    .who a:hover{ text-decoration:underline; }

    .deck{
      display:grid;
      grid-template-columns:1.4fr .9fr;
      gap:16px;
    }
    @media (max-width:920px){ .deck{ grid-template-columns:1fr; } }

    .card{
      position:relative;
      overflow:hidden;
      border-radius:30px;
      background:
        linear-gradient(135deg,
          rgba(255,0,153,.12),
          rgba(0,255,255,.08),
          rgba(255,255,0,.07),
          rgba(138,46,255,.12));
      border:2px solid rgba(255,255,255,.16);
      box-shadow:
        0 0 24px rgba(255,0,153,.18),
        0 0 46px rgba(0,255,255,.14),
        0 0 82px rgba(138,46,255,.12),
        inset 0 0 30px rgba(255,255,255,.04);
      backdrop-filter: blur(16px) saturate(1.5);
    }

    .card::before{
      content:"";
      position:absolute;
      inset:-2px;
      border-radius:inherit;
      padding:2px;
      background:linear-gradient(120deg,#ff00a8,#00f7ff,#ffe600,#8a2eff,#ff5e00,#ff00a8);
      background-size:280% 280%;
      animation:borderFlow 5.2s linear infinite;
      -webkit-mask: linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
      -webkit-mask-composite: xor;
              mask-composite: exclude;
      pointer-events:none;
    }

    .card-h{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      padding:16px 18px;
      border-bottom:1px solid rgba(255,255,255,.10);
      position:relative;
      z-index:1;
    }

    .card-h h2{
      margin:0;
      font-size:1.06rem;
      font-weight:900;
      text-transform:uppercase;
      letter-spacing:.12em;
      color:#fff;
      text-shadow:0 0 12px rgba(255,0,168,.4), 0 0 22px rgba(0,247,255,.2);
    }

    .card-b{
      padding:16px 18px 18px;
      position:relative;
      z-index:1;
    }

    .subtle, .muted, .k{
      color:rgba(255,255,255,.78) !important;
    }

    .grid{ display:grid; gap:12px; }
    .cols2{ grid-template-columns:1fr 1fr; }
    @media (max-width:720px){ .cols2{ grid-template-columns:1fr; } }

    label{
      display:block;
      margin:0 0 6px;
      font-weight:800;
      color:#fff;
      letter-spacing:.02em;
      text-shadow:0 0 10px rgba(255,255,255,.08);
    }

    .input, select{
      width:100%;
      padding:.82rem 1rem;
      border-radius:16px;
      border:1px solid rgba(255,255,255,.18);
      background:rgba(12,10,30,.62);
      color:#fff;
      box-sizing:border-box;
      outline:none;
      box-shadow:
        inset 0 0 18px rgba(255,255,255,.03),
        0 0 16px rgba(0,0,0,.08);
      backdrop-filter: blur(8px);
    }

    .input::placeholder{ color:rgba(255,255,255,.52); }

    .input:focus, select:focus{
      border-color:#00f7ff;
      box-shadow:
        0 0 0 4px rgba(0,247,255,.16),
        0 0 18px rgba(255,0,168,.16),
        inset 0 0 18px rgba(255,255,255,.04);
    }

    select option{
      color:#fff;
      background:#180325;
    }

    .toggle{
      display:flex;
      gap:16px;
      align-items:center;
      flex-wrap:wrap;
    }

    .toggle label{
      display:flex;
      gap:8px;
      align-items:center;
      cursor:pointer;
      padding:.75rem 1rem;
      border-radius:999px;
      background:rgba(255,255,255,.06);
      border:1px solid rgba(255,255,255,.14);
      font-weight:800;
    }

    .toggle input[type="radio"]{
      accent-color:#ff00a8;
      transform:scale(1.1);
    }

    .picker{
      display:flex;
      flex-direction:column;
      gap:6px;
    }

    .picker-ctl{
      position:relative;
      display:flex;
      align-items:center;
      gap:10px;
      border:1px solid rgba(255,255,255,.16);
      border-radius:18px;
      background:rgba(12,10,30,.56);
      min-height:50px;
      padding:0 10px;
      box-shadow: inset 0 0 18px rgba(255,255,255,.03);
      backdrop-filter: blur(10px);
    }

    .picker-ctl input[type=file]{
      position:absolute;
      inset:0;
      opacity:0;
      cursor:pointer;
    }

    .btn{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:.45rem;
      padding:.85rem 1.15rem;
      border:0;
      border-radius:999px;
      background:linear-gradient(90deg,#ff00a8,#8a2eff,#00f7ff,#ffe600,#ff00a8);
      background-size:280% 280%;
      color:#fff;
      font-weight:900;
      letter-spacing:.04em;
      text-transform:uppercase;
      cursor:pointer;
      box-shadow:
        0 0 18px rgba(255,0,168,.24),
        0 0 32px rgba(0,247,255,.18);
      animation:rainbowMove 5s linear infinite;
      transition:transform .16s ease, filter .16s ease;
    }

    .btn:hover{
      transform:translateY(-2px) scale(1.02);
      filter:brightness(1.08);
    }

    .btn.ghost{
      background:rgba(255,255,255,.08);
      color:#fff;
      border:1px solid rgba(255,255,255,.16);
      box-shadow:none;
      text-transform:none;
      letter-spacing:0;
      animation:none;
    }

    .btn.sm{
      padding:.65rem .95rem;
      font-size:.86rem;
      border-radius:999px;
    }

    .ellipsis{
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
      color:rgba(255,255,255,.72);
    }

    .rowc{
      display:grid;
      grid-template-columns:24px 1fr 110px 96px;
      align-items:center;
      gap:10px;
      padding:10px 12px;
      border:1px solid rgba(255,255,255,.12);
      border-radius:18px;
      background:
        linear-gradient(135deg,
          rgba(255,255,255,.07),
          rgba(255,255,255,.03));
      box-shadow:
        0 0 18px rgba(255,255,255,.03),
        inset 0 0 20px rgba(255,255,255,.03);
    }

    .ico{
      width:24px;
      height:24px;
      border-radius:8px;
      background:linear-gradient(135deg,#ffe600,#ff00a8,#00f7ff);
      box-shadow:0 0 12px rgba(255,255,255,.14), 0 0 22px rgba(255,0,168,.16);
    }

    .size,.eta{
      text-align:right;
      color:rgba(255,255,255,.76);
      font-variant-numeric:tabular-nums;
    }

    .progress{
      height:12px;
      border-radius:999px;
      overflow:hidden;
      border:1px solid rgba(255,255,255,.14);
      background:rgba(255,255,255,.08);
      margin-top:.45rem;
      box-shadow: inset 0 0 10px rgba(255,255,255,.03);
    }

    .progress > i{
      display:block;
      height:100%;
      width:0%;
      background:
        linear-gradient(90deg,#ff00a8,#8a2eff,#00f7ff,#ffe600,#ff00a8);
      background-size:220% 220%;
      animation:rainbowMove 3s linear infinite;
      position:relative;
    }

    .progress > i::after{
      content:"";
      position:absolute;
      inset:0;
      background-image:
        linear-gradient(135deg,
          rgba(255,255,255,.30) 25%,
          transparent 25%,
          transparent 50%,
          rgba(255,255,255,.30) 50%,
          rgba(255,255,255,.30) 75%,
          transparent 75%,
          transparent);
      background-size:24px 24px;
      animation:stripes 1s linear infinite;
      mix-blend-mode:screen;
    }

    .kv{
      display:grid;
      grid-template-columns:1fr 1fr;
      gap:12px;
    }
    .kv .v{
      font-weight:900;
      color:#fff;
      font-variant-numeric:tabular-nums;
      text-shadow:0 0 12px rgba(255,0,168,.24), 0 0 20px rgba(0,247,255,.16);
    }

    @media(max-width:420px){ .kv{ grid-template-columns:1fr; } }

    .log{
      max-height:220px;
      overflow:auto;
      border:1px solid rgba(255,255,255,.12);
      border-radius:16px;
      background:rgba(12,10,30,.48);
      padding:10px 12px;
      font-size:.92rem;
      color:rgba(255,255,255,.86);
      box-shadow: inset 0 0 18px rgba(255,255,255,.03);
    }
    .log p{ margin:4px 0; }

    .totalline{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:8px;
    }

    .badge{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      padding:.32rem .75rem;
      border-radius:999px;
      font-weight:900;
      font-size:.8rem;
      letter-spacing:.03em;
      border:1px solid rgba(255,255,255,.16);
      background:rgba(255,255,255,.08);
      color:#fff;
      text-shadow:0 0 10px rgba(255,255,255,.1);
    }

    .badge.ok{
      background:linear-gradient(90deg, rgba(0,255,170,.20), rgba(0,247,255,.20));
      color:#dff;
    }

    .badge.warn{
      background:linear-gradient(90deg, rgba(255,0,168,.18), rgba(255,230,0,.16));
      color:#fff7b3;
    }

    .share{
      display:flex;
      align-items:center;
      gap:8px;
    }
    .share .input{ padding:.72rem .85rem; }
    .share .btn{ padding:.72rem .95rem; }

    .footer{
      color:rgba(255,255,255,.72);
      margin-top:16px;
      text-align:center;
      font-size:.98rem;
      text-shadow:0 0 10px rgba(255,255,255,.06);
    }

    .blob{
      position:absolute;
      border-radius:50%;
      filter:blur(22px);
      opacity:.22;
      mix-blend-mode:screen;
      pointer-events:none;
      animation:blobMove 12s ease-in-out infinite;
    }
    .blob.b1{
      width:220px;height:220px;background:#ff00a8;top:-70px;left:-50px;
    }
    .blob.b2{
      width:180px;height:180px;background:#00f7ff;right:-50px;top:36px;animation-delay:-4s;
    }
    .blob.b3{
      width:240px;height:240px;background:#ffe600;bottom:-110px;left:42%;animation-delay:-7s;
    }

    @keyframes spinGlow{
      from{ transform:rotate(0deg) scale(1); }
      to{ transform:rotate(360deg) scale(1.06); }
    }
    @keyframes pulseRings{
      0%,100%{ transform:scale(1); opacity:.28; }
      50%{ transform:scale(1.08); opacity:.42; }
    }
    @keyframes driftColors{
      0%,100%{ background-position:0% 50%; }
      50%{ background-position:100% 50%; }
    }
    @keyframes borderFlow{
      0%{ background-position:0% 50%; }
      100%{ background-position:200% 50%; }
    }
    @keyframes rainbowMove{
      0%{ background-position:0% 50%; }
      100%{ background-position:200% 50%; }
    }
    @keyframes stripes{
      0%{ transform:translateX(0); }
      100%{ transform:translateX(24px); }
    }
    @keyframes blobMove{
      0%,100%{ transform:translate(0,0) scale(1); }
      33%{ transform:translate(20px,-16px) scale(1.12); }
      66%{ transform:translate(-16px,18px) scale(.92); }
    }

    @media (max-width:700px){
      .rowc{ grid-template-columns:1fr; }
      .size,.eta{ text-align:left; }
      .share{ flex-direction:column; align-items:stretch; }
    }

    @media (prefers-reduced-motion: reduce){
      .psy-overlay,
      .psy-overlay::before,
      .psy-overlay::after,
      .card::before,
      .btn,
      .progress > i,
      .blob{
        animation:none !important;
      }
    }
  </style>
</head>
<body>
<div class="psy-overlay"></div>

<div class="shell">
  <div class="hdr">
    <h1 class="brand">Psychedelische Upload</h1>

    <div class="right">
      <div class="who">Ingelogd als <strong>{{ user }}</strong> • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
    </div>
  </div>

  <div class="deck">
    <div class="card">
      <div class="blob b1"></div>
      <div class="blob b2"></div>
      <div class="blob b3"></div>

      <div class="card-h">
        <h2>Upload Portaal</h2>
        <div class="subtle">Parallel: <span id="kvWorkers">3</span></div>
      </div>

      <div class="card-b">
        <form id="form" class="grid" autocomplete="off" enctype="multipart/form-data">
          <div class="grid cols2">
            <div>
              <label>Uploadtype</label>
              <div class="toggle">
                <label><input type="radio" name="upmode" value="files" checked> Bestand(en)</label>
                <label id="folderLabel"><input type="radio" name="upmode" value="folder"> Map</label>
              </div>
            </div>
            <div>
              <label for="title">Onderwerp</label>
              <input id="title" class="input" type="text" placeholder="Bijv. Tekeningen project X" maxlength="120">
            </div>
          </div>

          <div class="grid cols2">
            <div>
              <label for="expDays">Verloopt na</label>
              <select id="expDays" class="input">
                <option value="1">1 dag</option>
                <option value="3">3 dagen</option>
                <option value="7">7 dagen</option>
                <option value="30" selected>30 dagen</option>
                <option value="60">60 dagen</option>
                <option value="365">1 jaar</option>
              </select>
            </div>
            <div>
              <label for="pw">Wachtwoord (optioneel)</label>
              <input id="pw" class="input" type="password" placeholder="Optioneel" autocomplete="new-password">
            </div>
          </div>

          <div id="fileRow" class="picker">
            <label for="fileInput">Kies bestand(en)</label>
            <div class="picker-ctl">
              <button type="button" id="btnFiles" class="btn ghost">Kies bestanden</button>
              <div id="fileName" class="ellipsis">Nog geen bestanden gekozen</div>
              <input id="fileInput" type="file" multiple>
            </div>
          </div>

          <div id="folderRow" class="picker" style="display:none">
            <label for="folderInput">Kies een map</label>
            <div class="picker-ctl">
              <button type="button" id="btnFolder" class="btn ghost">Kies map</button>
              <div id="folderName" class="ellipsis">Nog geen map gekozen</div>
              <input id="folderInput" type="file" multiple webkitdirectory directory>
            </div>
            <div class="muted" style="margin-top:2px">Tip: mapselectie werkt niet op iOS.</div>
          </div>

          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:4px">
            <button id="btnStart" class="btn" type="submit">Uploaden</button>
            <span class="muted">Queue: <span id="kvQueue">0</span> • Bestanden: <span id="kvFiles">0</span></span>
          </div>
        </form>

        <div id="queue" class="grid" style="margin-top:14px"></div>

        <div style="margin-top:14px">
          <div class="totalline">
            <div class="subtle">Totaalvoortgang</div>
            <span id="totalPct" class="badge warn">0%</span>
          </div>
          <div class="progress"><i id="totalFill"></i></div>
          <div class="subtle" id="totalStatus" style="margin-top:6px">Nog niet gestart</div>
        </div>

        <div id="result" style="margin-top:12px"></div>
      </div>
    </div>

    <div class="card">
      <div class="card-h">
        <h2>Live Telemetry</h2>
        <div class="subtle">Sessie</div>
      </div>

      <div class="card-b grid">
        <div class="kv">
          <div>
            <div class="k">Actieve workers</div>
            <div class="v" id="tWorkers">0</div>
          </div>
          <div>
            <div class="k">Doorvoersnelheid</div>
            <div class="v"><span id="tSpeed">0</span> /s</div>
          </div>
          <div>
            <div class="k">Verplaatst</div>
            <div class="v" id="tMoved">0 B</div>
          </div>
          <div>
            <div class="k">Nog te gaan</div>
            <div class="v" id="tLeft">0 B</div>
          </div>
          <div>
            <div class="k">ETA</div>
            <div class="v" id="tEta">—</div>
          </div>
          <div>
            <div class="k">Bestanden klaar</div>
            <div class="v" id="tDone">0</div>
          </div>
        </div>

        <div>
          <div class="k" style="margin-bottom:6px">Activiteitenlog</div>
          <div id="log" class="log" aria-live="polite"></div>
        </div>
      </div>
    </div>
  </div>

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
/* ==== Settings & iOS ==== */
const FILE_PAR = 3;
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);

/* Elements */
const folderLabel=document.getElementById('folderLabel');
const fileRow=document.getElementById('fileRow'), folderRow=document.getElementById('folderRow');
const fileInput=document.getElementById('fileInput'), folderInput=document.getElementById('folderInput');
const btnFiles=document.getElementById('btnFiles'), btnFolder=document.getElementById('btnFolder');
const fileName=document.getElementById('fileName'), folderName=document.getElementById('folderName');
const queue=document.getElementById('queue'); const form=document.getElementById('form');
const totalFill=document.getElementById('totalFill'), totalPct=document.getElementById('totalPct'), totalStatus=document.getElementById('totalStatus');
const kvWorkers=document.getElementById('kvWorkers'), kvQueue=document.getElementById('kvQueue'), kvFiles=document.getElementById('kvFiles');
const tWorkers=document.getElementById('tWorkers'), tSpeed=document.getElementById('tSpeed'), tMoved=document.getElementById('tMoved'), tLeft=document.getElementById('tLeft'), tEta=document.getElementById('tEta'), tDone=document.getElementById('tDone');
const logEl=document.getElementById('log'), resBox=document.getElementById('result');

if(isIOS){ folderLabel.style.display='none'; }

/* Utils */
function fmtBytes(n){const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):Math.round(n))+" "+u[i]}
function log(msg){const p=document.createElement('p');const t=new Date().toLocaleTimeString();p.textContent=`[${t}] ${msg}`;logEl.prepend(p)}
function setTotal(p,label){const pct=Math.max(0,Math.min(100,p)); totalFill.style.width=pct+'%'; totalPct.textContent=Math.round(pct)+'%'; if(label) totalStatus.textContent=label;}

/* API */
async function packageInit(expiry,password,title){
  const r=await fetch("{{ url_for('package_init') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expiry_days:expiry,password,title})});
  const j=await r.json(); if(!j.ok) throw new Error(j.error||'init'); return j.token;
}
async function putInit(token,filename,type){
  const r=await fetch("{{ url_for('put_init') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,filename,contentType:type||'application/octet-stream'})});
  const j=await r.json(); if(!j.ok) throw new Error(j.error||'put_init'); return j;
}
async function putComplete(token,key,name,path){
  const r=await fetch("{{ url_for('put_complete') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,key,name,path})});
  const j=await r.json(); if(!j.ok) throw new Error(j.error||'put_complete'); return j;
}
function putWithProgress(url,blob,onProgress){
  return new Promise((resolve,reject)=>{
    const x=new XMLHttpRequest();
    x.open("PUT",url,true);
    x.setRequestHeader("Content-Type",blob.type||"application/octet-stream");
    x.upload.onprogress=(e)=>{const loaded=e.loaded||0,total=e.total||blob.size||1;onProgress(loaded,total);};
    x.onload=()=> (x.status>=200&&x.status<300)?resolve():reject(new Error('HTTP '+x.status));
    x.onerror=()=>reject(new Error('Netwerkfout')); x.send(blob);
  });
}

/* Queue rows */
function addRow(rel,size){
  const r=document.createElement('div'); r.className='rowc';
  r.innerHTML=`<div class="ico"></div>
               <div class="ellipsis"><strong>${rel}</strong><div class="progress"><i style="width:0%"></i></div></div>
               <div class="size">${fmtBytes(size)}</div>
               <div class="eta" data-eta>—</div>`;
  queue.appendChild(r);
  return {row:r,fill:r.querySelector('i'),eta:r.querySelector('[data-eta]')};
}

/* Telemetry state */
let totBytes=0, moved=0, done=0, workers=0;
let speedAvg=0; let lastTick=performance.now(), lastMoved=0;
setInterval(()=>{
  const now=performance.now(); const dt=(now-lastTick)/1000; lastTick=now;
  const delta = moved - lastMoved; lastMoved = moved;
  const inst = delta / Math.max(dt,0.001);
  speedAvg = speedAvg ? speedAvg*0.7 + inst*0.3 : inst;
  tSpeed.textContent = fmtBytes(speedAvg)+"/s";
  tMoved.textContent = fmtBytes(moved);
  tLeft.textContent  = fmtBytes(Math.max(0, totBytes - moved));
  tWorkers.textContent = workers;
  tDone.textContent = done;
  const etaSec = speedAvg>1 ? Math.max(0,(totBytes-moved)/speedAvg) : 0;
  tEta.textContent = (totBytes && speedAvg>1) ? new Date(etaSec*1000).toISOString().substring(11,19) : "—";
}, 700);

/* UI bindings */
(function initPickers(){
  const fileCounterEls = [kvFiles, kvQueue].filter(Boolean);
  const setCounters = (n) => fileCounterEls.forEach(el => el.textContent = String(n));

  const fileSummary = (files, emptyText) => {
    const n = files?.length || 0;
    if (!n) return emptyText;
    const names = Array.from(files).slice(0, 2).map(f => f.name);
    const more = n > 2 ? ` … (+${n - 2})` : "";
    return names.join(", ") + more;
  };

  const folderSummary = (files) => {
    const n = files?.length || 0;
    if (!n) return "Nog geen map gekozen";
    const first = files[0];
    const rel = first?.webkitRelativePath || "";
    const root = rel.split("/")[0] || "Gekozen map";
    return `${root} (${n} bestanden)`;
  };

  const openPicker = (inputEl) => {
    if (!inputEl) return;
    try { inputEl.click(); } catch(_) {}
  };

  const setMode = (mode) => {
    const useFolder = (mode === "folder" && !isIOS);
    if (fileRow) fileRow.style.display = useFolder ? "none" : "";
    if (folderRow) folderRow.style.display = useFolder ? "" : "none";
    setTimeout(() => openPicker(useFolder ? folderInput : fileInput), 0);
  };

  if (btnFiles && fileInput) btnFiles.addEventListener("click", () => openPicker(fileInput));
  if (btnFolder && folderInput) btnFolder.addEventListener("click", () => openPicker(folderInput));

  if (fileInput){
    fileInput.addEventListener("change", () => {
      const n = fileInput.files?.length || 0;
      setCounters(n);
      if (fileName) fileName.textContent = fileSummary(fileInput.files, "Nog geen bestanden gekozen");
    });
  }

  if (folderInput){
    folderInput.addEventListener("change", () => {
      const n = folderInput.files?.length || 0;
      setCounters(n);
      if (folderName) folderName.textContent = folderSummary(folderInput.files);
    });
  }

  document.querySelectorAll('input[name=upmode]').forEach(radio => {
    radio.addEventListener("change", (e) => setMode(e.target.value));
  });

  const current = document.querySelector('input[name=upmode]:checked')?.value || "files";
  setMode(current);
})();

/* Main submit */
form.addEventListener('submit', async (e)=>{
  e.preventDefault();
  queue.innerHTML=''; moved=0; done=0; speedAvg=0; setTotal(0,'Voorbereiden…');
  const mode=document.querySelector('input[name=upmode]:checked').value;
  const useFolder = mode==='folder' && !isIOS;
  const files = Array.from(useFolder ? folderInput.files : fileInput.files);
  if(!files.length){ alert("Kies eerst "+(useFolder?"een map":"bestanden")+"."); return; }
  const expiry=document.getElementById('expDays').value, pw=document.getElementById('pw').value||'', title=document.getElementById('title').value||'';
  const token = await packageInit(expiry,pw,title);

  totBytes = files.reduce((s,f)=>s+f.size,0)||1;
  kvQueue.textContent = files.length; kvFiles.textContent = files.length;
  const list = files.map(f=>({f,rel:useFolder?(f.webkitRelativePath||f.name):f.name,ui:addRow(useFolder?(f.webkitRelativePath||f.name):f.name,f.size),start:0,uploaded:0}));
  const q=[...list];

  async function worker(){
    workers++; kvWorkers.textContent=FILE_PAR; try{
      while(q.length){
        const it=q.shift();
        it.start=performance.now(); log("Start: "+it.rel);
        try{
          const init=await putInit(token,it.f.name,it.f.type);
          let last=0;
          await putWithProgress(init.url,it.f,(loaded,total)=>{
            const pct=Math.round(loaded/total*100);
            it.ui.fill.style.width=pct+'%';
            const d=loaded-last; last=loaded; moved+=d; it.uploaded=loaded;
            const spent=(performance.now()-it.start)/1000; const sp = loaded/Math.max(spent,0.001);
            const left=total-loaded; const etaS= sp>1 ? left/sp : 0; it.ui.eta.textContent = etaS? new Date(etaS*1000).toISOString().substring(11,19) : '—';
            setTotal(moved/totBytes*100,'Uploaden…');
          });
          await putComplete(token,init.key,it.f.name,it.rel);
          it.ui.fill.style.width='100%'; it.ui.eta.textContent='Klaar'; done++; log("Klaar: "+it.rel);
        }catch(err){ it.ui.eta.textContent='Fout'; log("Fout: "+it.rel); }
      }
    } finally { workers--; }
  }
  await Promise.all(Array.from({length:Math.min(FILE_PAR,list.length)}, worker));
  setTotal(100,'Klaar');

  const link="{{ url_for('package_page', token='__T__', _external=True) }}".replace("__T__", token);
  resBox.innerHTML = `<div class="card" style="margin-top:8px"><div class="card-b">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:8px;flex-wrap:wrap">
      <div class="subtle" style="font-weight:900">Deelbare link</div><span class="badge ok">Gereed</span>
    </div>
    <div class="share">
      <input id="shareLinkInput" class="input" value="${link}" readonly>
      <button id="copyBtn" type="button" class="btn sm">Kopieer</button>
    </div>
  </div></div>`;

  document.getElementById('copyBtn').onclick = async () => {
    const input = document.getElementById('shareLinkInput');
    const btn = document.getElementById('copyBtn');
    const oldText = btn.textContent;
    btn.textContent = "Kopiëren…";
    btn.disabled = true;

    try {
      await navigator.clipboard.writeText(input.value);
      btn.textContent = "Gekopieerd ✓";
    } catch (_) {
      input.focus();
      input.select();
      const ok = document.execCommand('copy');
      btn.textContent = ok ? "Gekopieerd ✓" : "Kopieer handmatig";
    } finally {
      setTimeout(() => {
        btn.textContent = oldText;
        btn.disabled = false;
      }, 1200);
    }
  };
});
</script>
</body>
</html>
"""


PACKAGE_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1"/>
  <title>Download • Olde Hanter</title>
  {{ head_icon|safe }}
  <style>
    {{ base_css }}

    /* ===== Downloadlijst: nette uitlijning ===== */
    .filecard{
      display:grid;
      grid-template-columns: 1fr auto auto; /* Naam | Size | Link */
      align-items:center;
      gap:.6rem .8rem;
      padding:.70rem 1rem;
      border:1px solid var(--line);
      border-radius:12px;
      background:color-mix(in oklab,var(--surface) 86%,white 14%);
    }
    .filecard .name{
      min-width:0;
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
      line-height:1.25;
      overflow-wrap:anywhere;
    }
    .filecard .size{ width:6.5rem; text-align:right; }
    .filecard .action{ width:auto; text-align:right; }
    .filecard .action a{ display:inline-block; padding:.2rem .4rem; white-space:nowrap; }


/* Header actions netjes uitlijnen */
.hdr .actions{
  display:flex;
  align-items:center;
  justify-content:flex-end;
  gap:10px;
  flex-wrap:wrap;
}

/* Kleine zwarte knop */
.btn-dark{
  display:inline-flex;
  align-items:center;
  gap:.45rem;
  padding:.55rem .75rem;      /* kleiner */
  border-radius:11px;
  background:#0b0b0b;         /* zwart */
  color:#fff;
  text-decoration:none;
  font-weight:800;
  line-height:1;
  border:1px solid rgba(255,255,255,.12);
}
.btn-dark:hover{ filter:brightness(1.08); }
.btn-dark:active{ transform:translateY(1px); }

.btn-dark .ic{ font-size:1.05em; opacity:.9; }

/* Optioneel: “Nieuwe aanvraag” als rustige link-knop */
.btn-link{
  display:inline-flex;
  align-items:center;
  gap:.45rem;
  padding:.55rem .7rem;
  border-radius:11px;
  border:1px solid var(--line);
  background:var(--surface);
  color:var(--text);
  text-decoration:none;
  font-weight:800;
  line-height:1;
}


    /* Progressbalk ruimte */
    #bar{ margin-top:.75rem }

    /* Kaarten & grid */
    .shell{max-width:980px;margin:5vh auto;padding:0 16px}
.hdr{
  display:flex;
  align-items:center;
  justify-content:space-between;
  margin-bottom:32px;
  gap:10px;
  flex-wrap:wrap;
}

    .brand{color:var(--brand);margin:0;font-weight:800}
    .deck{display:grid;grid-template-columns:2fr 1fr;gap:14px}
    @media(max-width:900px){.deck{grid-template-columns:1fr}}
    .card{border-radius:16px;background:var(--panel);border:1px solid var(--panel-b);box-shadow:0 14px 36px rgba(0,0,0,.14);overflow:hidden}
    .card-h{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid rgba(0,0,0,.06)}
    .card-b{padding:14px 16px}
    .subtle{color:var(--muted);font-size:.92rem}
    .btn{padding:.7rem 1rem;border:0;border-radius:11px;background:linear-gradient(180deg,var(--brand),color-mix(in oklab,var(--brand)85%,black 15%));color:#fff;font-weight:800;cursor:pointer}
    .progress{height:10px;border-radius:999px;overflow:hidden;border:1px solid #dbe5f4;background:#eef2ff}
    .progress>i{display:block;height:100%;width:0%;background:linear-gradient(90deg,#0f4c98,#1e90ff);transition:width .12s}
    .progress.indet>i{width:40%;animation:ind 1.1s linear infinite}
    @keyframes ind{0%{transform:translateX(-100%)}100%{transform:translateX(240%)}}

    .kv{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .kv .k{font-size:.85rem;color:var(--muted)} .kv .v{font-weight:800;font-variant-numeric:tabular-nums}

    .deck > .card { min-width: 0; }
    .card h1, .card h2, .card h3 { line-height: 1.2; }
    .card p, .card li, .card div { line-height: 1.25; }

    @media (max-width:700px){
      .filecard{grid-template-columns:1fr;gap:.35rem;}
      .filecard .name{white-space:normal;}
      .filecard .size,.filecard .action{width:auto;display:flex;justify-content:space-between;gap:.6rem;}
    }
  </style>
</head>
<body>
{{ bg|safe }}
<div class="shell">
<div class="actions">
  <a class="btn-dark" href="{{ url_for('contact') }}">
    <span class="ic">★</span> Abonnement aanvragen
  </a>
</div>


  <div class="deck">
    <!-- Linkerkaart -->
    <div class="card">
      <div class="card-h"><div>Download</div><div class="subtle">Bestanden: {{ items|length }}</div></div>
      <div class="card-b">
        {% if items|length == 1 %}
          <button id="btnDownload" class="btn">Download bestand</button>
        {% else %}
          <button id="btnDownload" class="btn">Alles downloaden (ZIP)</button>
        {% endif %}
        <div id="bar" class="progress" style="display:none"><i></i></div>
        <div class="subtle" id="txt" style="margin-top:6px;display:none">Starten…</div>

        {% if items|length > 1 %}
        <h4 style="margin-top:14px;">Inhoud</h4>
        {% for it in items %}
          <div class="filecard">
            <div class="name">{{ it["path"] }}</div>
            <div class="size">{{ it["size_h"] }}</div>
            <div class="action"><a class="subtle" href="{{ url_for('stream_file', token=token, item_id=it['id']) }}">los</a></div>
          </div>
        {% endfor %}
        {% endif %}
      </div>
    </div>

    <!-- Rechterkaart -->
    <div class="card">
      <div class="card-h"><div>Live Telemetry</div><div class="subtle">Sessie</div></div>
      <div class="card-b kv">
        <div class="k">Doorvoersnelheid</div><div class="v" id="tSpeed">0 B/s</div>
        <div class="k">Gedownload</div><div class="v" id="tMoved">0 B</div>
        <div class="k">Totale grootte</div><div class="v" id="tTotal">{{ total_human }}</div>
        <div class="k">ETA</div><div class="v" id="tEta">—</div>
      </div>
    </div>
  </div>

  <p class="footer" style="text-align:center;margin-top:14px">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
const bar=document.getElementById('bar'), fill=bar?bar.querySelector('i'):null, txt=document.getElementById('txt');
const tSpeed=document.getElementById('tSpeed'), tMoved=document.getElementById('tMoved'), tEta=document.getElementById('tEta');
function fmtBytes(n){const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):Math.round(n))+" "+u[i]}
function show(){bar.style.display='block';txt.style.display='block'}
function setPct(p){if(fill){fill.style.width=Math.max(0,Math.min(100,p))+'%'}}

async function downloadWithTelemetry(url, fallbackName){
  show(); setPct(0); txt.textContent='Starten…';
  let speedAvg=0, lastT=performance.now(), lastB=0, moved=0, total=0;

  const tick = ()=>{ const now=performance.now(), dt=(now-lastT)/1000; lastT=now; const inst=(moved-lastB)/Math.max(dt,0.001); lastB=moved; speedAvg = speedAvg? speedAvg*0.7 + inst*0.3 : inst; tSpeed.textContent=fmtBytes(speedAvg)+'/s'; const eta = (total && speedAvg>1) ? (total-moved)/speedAvg : 0; tEta.textContent = eta? new Date(eta*1000).toISOString().substring(11,19) : '—'; };
  const iv=setInterval(tick,700);

  try{
    const res=await fetch(url,{credentials:'same-origin'});
    if(!res.ok){ txt.textContent='Fout '+res.status; clearInterval(iv); return; }
    total=parseInt(res.headers.get('Content-Length')||'0',10);
    const name=res.headers.get('X-Filename')||fallbackName||'download';

    const rdr = res.body && res.body.getReader ? res.body.getReader() : null;
    if(rdr){
      const chunks=[];
      if(!total){ bar.classList.add('indet'); txt.textContent='Downloaden…'; }
      while(true){
        const {done,value}=await rdr.read(); if(done) break;
        chunks.push(value); moved+=value.length; tMoved.textContent=fmtBytes(moved);
        if(total){ setPct(Math.round(moved/total*100)); txt.textContent=Math.round(moved/total*100)+'%'; }
      }
      if(!total){ bar.classList.remove('indet'); setPct(100); txt.textContent='Klaar'; }
      clearInterval(iv);
      const blob=new Blob(chunks); const u=URL.createObjectURL(blob);
      const a=document.createElement('a'); a.href=u; a.download=name; a.rel='noopener'; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(u);
      return;
    }
    bar.classList.add('indet'); txt.textContent='Downloaden…';
    const blob=await res.blob(); clearInterval(iv); bar.classList.remove('indet'); setPct(100); txt.textContent='Klaar';
    const u=URL.createObjectURL(blob); const a=document.createElement('a'); a.href=u; a.download=fallbackName||'download'; a.click(); URL.revokeObjectURL(u);
  }catch(e){ clearInterval(iv); txt.textContent='Fout'; }
}

const btn=document.getElementById('btnDownload');
if(btn){
  btn.addEventListener('click',()=>{
    {% if items|length == 1 %}
      downloadWithTelemetry("{{ url_for('stream_file', token=token, item_id=items[0]['id']) }}","{{ items[0]['name'] }}");
    {% else %}
      downloadWithTelemetry("{{ url_for('stream_zip', token=token) }}","{{ (title or ('pakket-'+token)) + ('.zip' if not title or not title.endswith('.zip') else '') }}");
    {% endif %}
  });
}
</script>
</body>
</html>
"""



CONTACT_HTML = r"""
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eigen transfer-oplossing – downloadlink.nl</title>{{ head_icon|safe }}

<style>
  {{ base_css }}

  /* ---------- Page-specific base styles ---------- */
  .form-actions{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;margin-top:1rem}
  .notice{display:block;margin-top:.5rem;color:#334155}
  .helper{font-size:.9rem;color:#475569;margin-top:.35rem}
  .section-gap{margin-top:1rem}
  .divider{height:1px;background:#e5e7eb;margin:1.2rem 0}
  @media (max-width:680px){.form-actions{gap:.5rem}}

  /* =====================================================
     CONTACT PAGE — SOLID, READABLE CARD (light & dark)
     ===================================================== */
  .card.contact-card{
    /* light mode: bijna opaak */
    background: rgba(255,255,255,0.9) !important;
    color: #0f172a !important;
    border: 1px solid rgba(0,0,0,0.08) !important;
    border-radius: 20px;
    box-shadow: 0 18px 40px rgba(0,0,0,.18);
    backdrop-filter: blur(12px) saturate(1.2);
  }

  /* Koppen en labels */
  .card.contact-card h1,
  .card.contact-card label{
    color:#0f172a !important;
  }

  /* Subteksten */
  .card.contact-card .small,
  .card.contact-card .helper,
  .card.contact-card .notice{
    color:#334155 !important;
  }

  /* Invoervelden (goed contrast, geen transparantie) */
  .card.contact-card .input,
  .card.contact-card input[type=text],
  .card.contact-card input[type=email],
  .card.contact-card input[type=tel],
  .card.contact-card input[type=password],
  .card.contact-card select,
  .card.contact-card textarea{
    color:#0f172a !important;
    background:#f8fafc !important;
    border:1px solid #cbd5e1 !important;
  }
  .card.contact-card input::placeholder,
  .card.contact-card textarea::placeholder{
    color:#6b7280 !important;
  }
  .card.contact-card .input:focus,
  .card.contact-card input:focus,
  .card.contact-card select:focus,
  .card.contact-card textarea:focus{
    border-color:#2563eb !important;
    box-shadow:0 0 0 4px color-mix(in oklab, #2563eb 25%, transparent) !important;
    outline:0;
  }

  /* Divider, links en knoppen */
  .card.contact-card .divider{ background:#e5e7eb !important; }
  .card.contact-card a{ color:#0f4c98 !important; text-decoration: underline; }
  .card.contact-card .btn{
    background: linear-gradient(180deg,#4a9fff,#1c62d2) !important;
    color:#fff !important;
  }

  /* ---------------- Dark mode variant ---------------- */
  @media (prefers-color-scheme: dark){
    .card.contact-card{
      background: rgba(15,23,42,0.92) !important; /* bijna opaak donker */
      color:#e5e7eb !important;
      border:1px solid rgba(255,255,255,0.14) !important;
      box-shadow: 0 18px 40px rgba(0,0,0,.42);
    }
    .card.contact-card h1,
    .card.contact-card label{ color:#e5e7eb !important; }

    .card.contact-card .small,
    .card.contact-card .helper,
    .card.contact-card .notice{ color:#9aa3b2 !important; }

    .card.contact-card .input,
    .card.contact-card input[type=text],
    .card.contact-card input[type=email],
    .card.contact-card input[type=tel],
    .card.contact-card input[type=password],
    .card.contact-card select,
    .card.contact-card textarea{
      background:#0f172a !important;
      border:1px solid #374151 !important;
      color:#e5e7eb !important;
    }
    .card.contact-card input::placeholder,
    .card.contact-card textarea::placeholder{ color:#9aa3b2 !important; }

    .card.contact-card .divider{ background:#1f2937 !important; }
    .card.contact-card a{ color:#7db4ff !important; }
  }
</style>


</head><body>
{{ bg|safe }}
<div class="wrap"><div class="card contact-card">
  <h1>Eigen transfer-oplossing aanvragen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}

  <form method="post" action="{{ url_for('contact') }}" novalidate id="contactForm">
    <div class="cols-2" style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label for="login_email">Gewenste inlog-e-mail</label>
        <input id="login_email" class="input" name="login_email" type="email" placeholder="naam@bedrijf.nl" value="{{ form.login_email or '' }}" required>
      </div>
      <div>
        <label for="storage_tb">Gewenste opslaggrootte</label>
        <select id="storage_tb" class="input" name="storage_tb" required>
          <option value="" {% if not form.storage_tb %}selected{% endif %}>Maak een keuze…</option>
          <option value="0.5" {% if form.storage_tb=='0.5' %}selected{% endif %}>0,5 TB — €12/maand</option>
          <option value="1"   {% if form.storage_tb=='1' %}selected{% endif %}>1 TB — €15/maand</option>
          <option value="2"   {% if form.storage_tb=='2' %}selected{% endif %}>2 TB — €20/maand</option>
          <option value="5"   {% if form.storage_tb=='5' %}selected{% endif %}>5 TB — €30/maand</option>
          <option value="more" {% if form.storage_tb=='more' %}selected{% endif %}>Meer opslag (op aanvraag)</option>
        </select>
        <div id="more-note" class="helper" style="display:none">
          Vul bij <strong>Opmerking</strong> de gewenste grootte of opties in.
        </div>
      </div>
    </div>

    <div class="cols-2 section-gap" style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label for="company">Bedrijfsnaam</label>
        <input id="company" class="input" name="company" type="text" placeholder="Bedrijfsnaam BV" value="{{ form.company or '' }}" minlength="2" maxlength="100" required>
        <div class="helper">
          Voorbeeld link: <code id="subPreview">{{ form.company and form.company or '' }}</code>
        </div>
      </div>
      <div>
        <label for="phone">Telefoonnummer</label>
        <input id="phone" class="input" name="phone" type="tel" placeholder="+31 6 12345678" value="{{ form.phone or '' }}" pattern="^[0-9+()\\s-]{8,20}$" required>
      </div>
    </div>

    <div class="cols-2 section-gap" style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label for="desired_password">Wachtwoord (voor jouw omgeving)</label>
        <input id="desired_password" class="input" name="desired_password" type="password" placeholder="Kies een sterk wachtwoord" minlength="6" required>
      </div>
      <div>
        <label for="notes">Opmerking (optioneel)</label>
        <input id="notes" class="input" name="notes" type="text" placeholder="Eventuele wensen/opmerkingen" maxlength="200" value="{{ form.notes or '' }}">
      </div>
    </div>

    <div class="divider"></div>

    <button class="btn" type="submit">Verstuur aanvraag</button>

    <span class="small notice">
      Door te versturen ga je akkoord met de
      <a href="{{ url_for('terms_page') }}" target="_blank" rel="noopener">Algemene voorwaarden</a>
      en onze <a href="{{ url_for('privacy_page') }}" target="_blank" rel="noopener">Privacyverklaring</a>.
    </span>

    <p class="small" style="margin-top:.8rem;color:#334155">
      Je omgeving wordt doorgaans binnen <strong>1–2 werkdagen</strong> actief.
      Na livegang ontvang je een <strong>bevestigingsmail</strong> met alle gegevens.
    </p>
  </form>

  <div id="paypalSection" style="display:none; margin-top:1.4rem">
    <h3 style="margin:0 0 .4rem 0">Direct starten met een abonnement via PayPal</h3>
    <p class="small" style="margin:.15rem 0 .8rem 0">
      De knop hieronder kiest automatisch het juiste abonnement op basis van je opslagkeuze, zodra alle velden geldig zijn.
    </p>
    <div id="paypal-button-container" style="max-width:360px"></div>
    <div id="paypal-hint" class="small" style="color:#991b1b; display:none; margin-top:.5rem">
      Geen PayPal-plan geconfigureerd voor deze opslaggrootte. Kies een andere grootte of rond je aanvraag af; wij sturen dan een incasso-link per e-mail.
    </div>
  </div>

  <p class="footer">downloadlink.nl • Bestandentransfer</p>
</div></div>

<!-- PayPal SDK -->
<script src="https://www.paypal.com/sdk/js?client-id={{ paypal_client_id }}&vault=true&intent=subscription" data-sdk-integration-source="button-factory"></script>

<script>
// ---------- helpers ----------
function slugify(s){
  return (s||"")
    .toLowerCase()
    .normalize('NFD').replace(/[\u0300-\u036f]/g,'')
    .replace(/&/g,' en ')
    .replace(/[^a-z0-9]+/g,'-')
    .replace(/^-+|-+$/g,'')
    .replace(/--+/g,'-')
    .substring(0, 50);
}
const company = document.getElementById('company');
const subPreview = document.getElementById('subPreview');
const BASE_DOMAIN = "{{ base_host }}";
function updatePreview(){ const s = slugify(company.value); subPreview.textContent = s ? (s + "." + BASE_DOMAIN) : BASE_DOMAIN; }
company?.addEventListener('input', updatePreview); updatePreview();

// ---------- plan map ----------
const PLAN_MAP = {
  "0.5": "{{ paypal_plan_0_5 }}",
  "1":   "{{ paypal_plan_1 }}",
  "2":   "{{ paypal_plan_2 }}",
  "5":   "{{ paypal_plan_5 }}"
};

// ---------- elements ----------
const form = document.getElementById('contactForm');
const paypalSection = document.getElementById('paypalSection');
const paypalContainerSel = '#paypal-button-container';
const paypalHint = document.getElementById('paypal-hint');
const storageSelect = document.getElementById('storage_tb');
const moreNote = document.getElementById('more-note');

// ---------- validatie ----------
const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
const PHONE_RE = /^[0-9+()\s-]{8,20}$/;

function currentPlanId(){
  const v = (storageSelect?.value || "");
  if (!v || v === "more") return "";
  return PLAN_MAP[v] || "";
}
function formIsValid(){
  const email = document.getElementById('login_email').value.trim();
  const storage = storageSelect?.value || "";
  const comp = document.getElementById('company').value.trim();
  const phone = document.getElementById('phone').value.trim();
  const pw = document.getElementById('desired_password').value;

  const ok =
    EMAIL_RE.test(email) &&
    storage && storage !== "more" &&
    comp.length >= 2 && comp.length <= 100 &&
    PHONE_RE.test(phone) &&
    (pw || "").length >= 6;

  return ok;
}

// Toon/verberg "meer" hint
function toggleMoreNote(){ if (moreNote) moreNote.style.display = (storageSelect?.value === "more") ? 'block' : 'none'; }

// Render of hide Paypal sectie
let renderedForPlan = ""; // onthoud voor welke plan-id de knop is gerenderd
function renderPaypalConditional(){
  toggleMoreNote();

  const ok = formIsValid();
  const planId = currentPlanId();

  if (!ok || !planId){
    // verberg hele sectie + leegmaken
    paypalSection.style.display = 'none';
    const el = document.querySelector(paypalContainerSel);
    if (el) el.innerHTML = "";
    paypalHint.style.display = 'none';
    renderedForPlan = "";
    return;
  }

  // sectie tonen
  paypalSection.style.display = 'block';

  if (!planId){
    paypalHint.style.display = 'block';
    const el = document.querySelector(paypalContainerSel);
    if (el) el.innerHTML = "";
    renderedForPlan = "";
    return;
  } else {
    paypalHint.style.display = 'none';
  }

  if (renderedForPlan === planId) return;

  const el = document.querySelector(paypalContainerSel);
  if(!window.paypal || !el){ return; }
  el.innerHTML = "";

  paypal.Buttons({
    style: { shape: 'rect', color: 'gold', layout: 'vertical', label: 'subscribe' },
    createSubscription: function(data, actions) {
      return actions.subscription.create({ plan_id: planId });
    },
    onApprove: async function(data, actions) {
      try{
        await fetch("{{ url_for('paypal_store_subscription') }}", {
          method: "POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify({
            subscription_id: data.subscriptionID,
            plan_value: (document.getElementById('storage_tb')?.value || "")
          })
        });
        alert("Bedankt! Je abonnement is gestart. ID: " + data.subscriptionID);
      }catch(e){
        alert("Abonnement gestart, maar opslaan in systeem mislukte. Neem contact op.");
      }
    }
  }).render(paypalContainerSel);

  renderedForPlan = planId;
}

// events
['input','change','blur'].forEach(evt => {
  form.addEventListener(evt, renderPaypalConditional, true);
});
window.addEventListener('load', renderPaypalConditional);
</script>

</body></html>
"""


CONTACT_DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag verstuurd</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card"><h1>Dank je wel!</h1>
<p>Je aanvraag is verstuurd. We nemen zo snel mogelijk contact met je op.</p>
<p class="small" style="margin-top:.35rem">
  Je omgeving wordt doorgaans binnen <strong>1–2 werkdagen</strong> actief.
  Na livegang ontvang je een bevestigingsmail met alle gegevens.
</p>
<p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div>
</body></html>
"""

CONTACT_MAIL_FALLBACK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card">
  <h1>Aanvraag gereed</h1>
  <p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p>
  <a class="btn" href="{{ mailto_link }}">Open e-mail</a>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

TERMS_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Algemene Voorwaarden – downloadlink.nl</title>{{ head_icon|safe }}
<style>
{{ base_css }}
h1{color:var(--brand);margin:.2rem 0 1rem}
h2{margin:1.2rem 0 .4rem}
h3{margin:1rem 0 .35rem}
.section{margin-bottom:1.1rem}
.small{color:#475569}
.card p{margin:.45rem 0}
ol,ul{margin:.4rem 0 .6rem 1.2rem}
code{background:#eef2ff;padding:.05rem .35rem;border-radius:.3rem}
</style></head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card">
    <h1>Algemene Voorwaarden – downloadlink.nl (B2B)</h1>
    <p class="small">Versie: 1.0 • Laatst bijgewerkt: 25-09-2025</p>

    <div class="section">
      <h2>1. Definities</h2>
      <p><strong>Leverancier</strong>: downloadlink.nl (hierna: “downloadlink.nl”).<br>
         <strong>Klant</strong>: de (rechts)persoon die de Dienst afneemt voor zakelijke doeleinden.<br>
         <strong>Dienst</strong>: de online bestands-transfer en hostingfunctionaliteit inclusief opslag bij een cloudprovider (S3-compatibel).<br>
         <strong>Abonnement</strong>: maandelijks terugkerende dienst tegen een vaste prijs per opslagbundel.<br>
         <strong>Data</strong>: alle door Klant geüploade of via de Dienst verwerkte bestanden/gegegevens.</p>
    </div>

    <div class="section">
      <h2>2. Toepasselijkheid en rangorde</h2>
      <p>Deze voorwaarden zijn van toepassing op alle offertes, abonnementen en gebruik van de Dienst door Klant (B2B). Afwijkingen gelden alleen indien schriftelijk overeengekomen. Bij strijdigheid tussen documenten geldt de volgende rangorde: (1) schriftelijke maatwerkafspraak, (2) deze voorwaarden incl. bijlagen, (3) online documentatie/prijsinformatie.</p>
    </div>

    <div class="section">
      <h2>3. Aanbod, totstandkoming en looptijd</h2>
      <ol>
        <li>Het aanbod (opslagbundels/prijzen) wordt op de website getoond of per e-mail bevestigd. Kennelijke fouten/drukfouten binden downloadlink.nl niet.</li>
        <li>De overeenkomst ontstaat bij (i) online bevestiging via de site (incl. PayPal-subscribe), of (ii) schriftelijke/e-mail acceptatie van een voorstel.</li>
        <li>Abonnementen hebben een looptijd van één (1) maand en worden stilzwijgend verlengd, tenzij opgezegd per het einde van de lopende periode.</li>
      </ol>
    </div>

    <div class="section">
      <h2>4. Prijzen, betaling en facturatie</h2>
      <ol>
        <li>Prijzen zijn exclusief btw en overige heffingen. Eventuele transactiekosten (bijv. PayPal) kunnen in rekening worden gebracht.</li>
        <li>Betaling gebeurt via de gekozen betaalmethode (o.a. PayPal-abonnement). Bij storno of mislukte incasso mag downloadlink.nl toegang schorsen tot betaling.</li>
        <li>downloadlink.nl kan prijzen wijzigen. Bij verhoging voor een lopend maandelijks abonnement wordt Klant minimaal 30 dagen vooraf geïnformeerd; Klant mag in dat geval per einde lopende termijn opzeggen.</li>
      </ol>
    </div>

    <div class="section">
      <h2>5. Gebruik, Fair Use en Acceptable Use</h2>
      <ol>
        <li>Klant gebruikt de Dienst zorgvuldig, conform wet- en regelgeving en deze voorwaarden.</li>
        <li><strong>Fair Use:</strong> verkeer en opslag moeten redelijk zijn binnen de afgenomen bundel. Excessief dataverkeer of oneigenlijk gebruik (bijv. public CDN-gebruik) kan worden begrensd of belast.</li>
        <li><strong>Verboden inhoud en handelingen:</strong> onrechtmatige, inbreukmakende, bedrieglijke of schadelijke content/activiteiten (waaronder malware, phishing, haatdragende of strafbare inhoud) zijn verboden.</li>
        <li>downloadlink.nl mag content blokkeren/verwijderen en accounts schorsen bij (vermoeden van) overtreding of bij bevel van een bevoegde autoriteit.</li>
      </ol>
    </div>

    <div class="section">
      <h2>6. Beschikbaarheid, onderhoud en wijzigingen</h2>
      <ol>
        <li>downloadlink.nl streeft naar hoge beschikbaarheid maar geeft geen gegarandeerde uptime, tenzij schriftelijk anders overeengekomen.</li>
        <li>Onderhoud (gepland of spoed) kan leiden tot tijdelijke onbeschikbaarheid. downloadlink.nl tracht dit te beperken en – indien redelijkerwijs mogelijk – vooraf te melden.</li>
        <li>downloadlink.nl mag de Dienst (technisch/functioneel) wijzigen om veiligheid, prestaties of kwaliteit te verbeteren. Materiële wijzigingen worden – indien relevant – gecommuniceerd.</li>
      </ol>
    </div>

    <div class="section">
      <h2>7. Beveiliging en back-ups</h2>
      <ol>
        <li>downloadlink.nl treft passende technische en organisatorische maatregelen die passen bij de aard van de Dienst en de stand van de techniek.</li>
        <li>Klant is zelf verantwoordelijk voor het kiezen van een sterk wachtwoord, het geheimhouden van inloggegevens en voor eigen externe back-ups.</li>
        <li>Tenzij uitdrukkelijk overeengekomen, omvat de Dienst geen garantie op back-ups of herstel van individuele bestanden.</li>
      </ol>
    </div>

    <div class="section">
      <h2>8. Privacy en gegevensverwerking (AVG)</h2>
      <ol>
        <li>Voor zover downloadlink.nl bij de Dienst persoonsgegevens verwerkt voor Klant, is downloadlink.nl verwerker en Klant verwerkingsverantwoordelijke.</li>
        <li>Op die verwerking is de <em>Bijlage A – Verwerkersovereenkomst</em> van toepassing en maakt deel uit van deze voorwaarden.</li>
      </ol>
    </div>

    <div class="section">
      <h2>9. Intellectuele eigendom</h2>
      <ol>
        <li>Alle rechten op de Dienst, software en documentatie berusten bij downloadlink.nl of diens licentiegevers.</li>
        <li>Data van Klant blijven eigendom van Klant. Klant verleent downloadlink.nl een beperkte licentie om Data te hosten, verwerken en weer te geven voor het uitvoeren van de Dienst.</li>
      </ol>
    </div>

    <div class="section">
      <h2>10. Schorsing en beëindiging</h2>
      <ol>
        <li>downloadlink.nl mag de toegang (tijdelijk) schorsen bij betalingsachterstand, veiligheidsrisico’s of (vermoeden van) overtreding.</li>
        <li>Beëindiging kan per einde abonnementsperiode. Bij beëindiging kan Data worden verwijderd. Klant is zelf verantwoordelijk voor tijdig exporteren.</li>
      </ol>
    </div>

    <div class="section">
      <h2>11. Aansprakelijkheid</h2>
      <ol>
        <li>Aansprakelijkheid van downloadlink.nl is beperkt tot directe schade en tot een bedrag gelijk aan de door Klant betaalde vergoedingen over de laatste twaalf (12) maanden voorafgaand aan de gebeurtenis (of €5.000,– indien hoger niet is betaald), per gebeurtenis en in totaal.</li>
        <li>Uitsluiting: gevolgschade, gederfde winst/omzet, verlies van Data, reputatieschade en boetes van derden zijn uitgesloten.</li>
        <li>Deze beperkingen gelden niet bij opzet of bewuste roekeloosheid van leidinggevenden van downloadlink.nl.</li>
      </ol>
    </div>

    <div class="section">
      <h2>12. Overmacht</h2>
      <p>Bij overmacht (o.a. storingen bij derden/cloudproviders, netwerk-/energie-uitval, DDoS, oorlog, overheidsmaatregelen) is downloadlink.nl niet gehouden tot schadevergoeding of nakoming zolang de overmacht voortduurt.</p>
    </div>

    <div class="section">
      <h2>13. Wijzigingen voorwaarden</h2>
      <p>downloadlink.nl mag deze voorwaarden wijzigen. Bij materiële wijzigingen wordt Klant redelijkerwijs geïnformeerd. Indien Klant niet akkoord is, kan hij per einde lopende maand opzeggen.</p>
    </div>

    <div class="section">
      <h2>14. Toepasselijk recht en forumkeuze</h2>
      <p>Nederlands recht is van toepassing. Geschillen worden voorgelegd aan de bevoegde rechter in het arrondissement Overijssel, locatie Almelo/Enschede.</p>
    </div>

    <div class="section">
      <h2>Bijlage A – Verwerkersovereenkomst (B2B)</h2>
      <h3>A1. Onderwerp en rollen</h3>
      <p>Deze bijlage regelt de verwerking van persoonsgegevens in het kader van de Dienst. Klant is verwerkingsverantwoordelijke; downloadlink.nl is verwerker.</p>

      <h3>A2. Verwerkingen</h3>
      <p>Doeleinden: leveren van bestandsopslag/transfer; beveiliging/continuïteit; support; facturatie. Categorieën betrokkenen en gegevens: door Klant bepaald. Duur: duur van de overeenkomst.</p>

      <h3>A3. Verplichtingen verwerker</h3>
      <ul>
        <li>Alleen verwerken op gedocumenteerde instructies van Klant.</li>
        <li>Passende beveiligingsmaatregelen (art. 32 AVG), inclusief versleutelde transportlagen en restrictieve toegang.</li>
        <li>Medeplichtige medewerkers zijn tot vertrouwelijkheid verplicht.</li>
        <li>Subverwerkers (o.a. S3-cloudprovider, e-mail/PayPal) mogen worden ingezet; op verzoek verstrekt downloadlink.nl een actueel overzicht. Verwerker legt subverwerkers vergelijkbare verplichtingen op.</li>
        <li>Melding van een inbreuk in verband met persoonsgegevens zonder onredelijke vertraging na constatering, met relevante informatie voor Klant.</li>
        <li>Redelijke assistentie bij AVG-verplichtingen van Klant (o.a. rechten van betrokkenen, DPIA), tegen redelijke vergoeding indien buiten de normale dienstverlening.</li>
        <li>Data na afloop verwijderen of retourneren, tenzij wetgeving opslag vereist.</li>
        <li>Audits: Klant mag (max. 1× per 12 maanden) een audit laten uitvoeren, na redelijke aankondiging, tijdens kantooruren, met minimale verstoring. Geheimhouding/veiligheidseisen gelden. Redelijke kosten zijn voor Klant.</li>
      </ul>

      <h3>A4. Internationale doorgifte</h3>
      <p>Indien doorgifte buiten de EER plaatsvindt, zorgt downloadlink.nl voor passende waarborgen (zoals EU-modelclausules) of een gelijkwaardige grondslag.</p>

      <h3>A5. Aansprakelijkheid</h3>
      <p>De aansprakelijkheidsbeperkingen uit artikel 11 van de voorwaarden zijn ook op deze bijlage van toepassing, voor zover rechtens toegestaan.</p>
    </div>

    <div class="section small">
      <p>Vragen? Neem contact op via <a href="mailto:{{ mail_to }}">{{ mail_to }}</a>.</p>
    </div>
  </div>
  <p class="footer">downloadlink.nl • Bestandentransfer</p>
</div>
</body></html>
"""

EXPIRED_HTML = r"""

<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
<title>Downloadlink verlopen</title>
{{ head_icon|safe }}

<style>
{{ base_css }}

:root{
  --bg1:#070014;
  --bg2:#120022;
  --panel:rgba(0,0,0,.42);
  --panel-border:rgba(255,255,255,.12);
  --text:#fff;
  --muted:rgba(255,255,255,.76);
  --cyan:#4df7ff;
  --pink:#ff4fd8;
  --purple:#9d6bff;
  --lime:#9dff7c;
  --gold:#ffd166;
  --danger:#ff5a8a;
}

html,body{
  margin:0;
  width:100%;
  height:100%;
  overflow:hidden;
  color:var(--text);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:
    radial-gradient(circle at 18% 20%, rgba(77,247,255,.18), transparent 28%),
    radial-gradient(circle at 82% 16%, rgba(255,79,216,.15), transparent 26%),
    radial-gradient(circle at 50% 88%, rgba(157,255,124,.10), transparent 28%),
    linear-gradient(180deg,var(--bg2),var(--bg1));
  touch-action:none;
}

body{ overscroll-behavior:none; }
canvas{ display:block; }

#gameWrap{ position:fixed; inset:0; }

#psyOverlay{
  position:fixed;
  inset:0;
  pointer-events:none;
  z-index:2;
  background:
    radial-gradient(circle at 20% 30%, rgba(77,247,255,.05), transparent 32%),
    radial-gradient(circle at 70% 30%, rgba(255,79,216,.05), transparent 30%),
    radial-gradient(circle at 50% 80%, rgba(157,255,124,.04), transparent 30%);
  mix-blend-mode:screen;
  animation:psy 10s linear infinite;
}
@keyframes psy{
  from{ filter:hue-rotate(0deg) saturate(1.05); }
  to{ filter:hue-rotate(360deg) saturate(1.22); }
}

#ui{
  position:fixed;
  top:14px;
  left:14px;
  z-index:20;
  background:var(--panel);
  border:1px solid var(--panel-border);
  border-radius:16px;
  padding:12px 14px;
  backdrop-filter:blur(10px);
  box-shadow:0 16px 34px rgba(0,0,0,.28);
  width:min(470px, calc(100vw - 28px));
  pointer-events:none;
}

#brand{
  display:flex;
  align-items:center;
  gap:10px;
  margin-bottom:8px;
}

#brandMark{
  width:38px;
  height:38px;
  border-radius:12px;
  display:grid;
  place-items:center;
  font-weight:800;
  background:
    radial-gradient(circle at 50% 30%, rgba(255,255,255,.22), transparent 28%),
    linear-gradient(180deg, rgba(77,247,255,.35), rgba(255,79,216,.18));
  border:1px solid rgba(255,255,255,.14);
  box-shadow:0 0 20px rgba(77,247,255,.16), 0 0 26px rgba(255,79,216,.14);
}

#brandText b{ display:block; font-size:14px; }
#brandText span{ display:block; color:var(--muted); font-size:12px; }

#hud{
  display:grid;
  grid-template-columns:repeat(4,minmax(70px,1fr));
  gap:8px;
}

.stat{
  background:rgba(255,255,255,.05);
  border:1px solid rgba(255,255,255,.08);
  border-radius:12px;
  padding:8px 10px;
}

.stat .label{
  font-size:10px;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:.08em;
  margin-bottom:4px;
}
.stat .value{
  font-size:16px;
  font-weight:800;
}

#msg{
  margin-top:8px;
  color:var(--muted);
  font-size:12px;
  line-height:1.35;
}

#weaponBar{
  position:fixed;
  left:50%;
  bottom:14px;
  transform:translateX(-50%);
  z-index:24;
  display:flex;
  gap:8px;
  pointer-events:none;
}
.weapon-chip{
  background:rgba(0,0,0,.42);
  border:1px solid rgba(255,255,255,.12);
  border-radius:999px;
  padding:8px 12px;
  font-size:12px;
  color:white;
  backdrop-filter:blur(10px);
  box-shadow:0 8px 18px rgba(0,0,0,.22);
}
.weapon-chip.active{
  box-shadow:0 0 0 1px rgba(77,247,255,.6), 0 0 18px rgba(77,247,255,.18);
}
.weapon-chip.fired{
  box-shadow:0 0 0 1px rgba(255,140,192,.7), 0 0 24px rgba(255,110,161,.28);
  transform:translateY(-2px) scale(1.03);
}

#centerMessage{
  position:fixed;
  z-index:30;
  left:50%;
  top:50%;
  transform:translate(-50%,-50%);
  width:min(620px, calc(100vw - 28px));
  background:rgba(0,0,0,.52);
  border:1px solid rgba(255,255,255,.12);
  border-radius:22px;
  padding:22px;
  text-align:center;
  backdrop-filter:blur(12px);
  box-shadow:0 20px 40px rgba(0,0,0,.34);
}

#centerMessage.hidden{ display:none; }

#centerMessage h1{ margin:0 0 8px; font-size:28px; }
#centerMessage p{ margin:8px 0; color:var(--muted); line-height:1.45; }

#nameRow{
  margin-top:14px;
  display:flex;
  justify-content:center;
}
#playerName{
  width:min(290px, 78vw);
  border-radius:12px;
  border:1px solid rgba(255,255,255,.14);
  padding:12px 14px;
  background:rgba(255,255,255,.08);
  color:#fff;
  outline:none;
  font-size:15px;
}

#startBtn,#restartBtn{
  margin-top:12px;
  border:0;
  border-radius:12px;
  padding:12px 18px;
  font-weight:800;
  cursor:pointer;
  color:#07101d;
  background:linear-gradient(180deg,#9fe0ff,#69b0ff);
  box-shadow:0 10px 24px rgba(77,247,255,.2);
}

#boardWrap{
  margin-top:16px;
  text-align:left;
  background:rgba(255,255,255,.04);
  border:1px solid rgba(255,255,255,.08);
  border-radius:16px;
  padding:12px;
}
#boardWrap h3{ margin:0 0 10px; font-size:14px; }
#leaderboard{
  margin:0;
  padding-left:20px;
  max-height:180px;
  overflow:auto;
}
#leaderboard li{
  margin:6px 0;
  color:var(--muted);
  font-size:14px;
}
.board-meta{
  display:flex;
  justify-content:space-between;
  gap:12px;
  color:rgba(255,255,255,.65);
  font-size:12px;
}

#crosshair{
  position:fixed;
  left:50%;
  top:50%;
  width:22px;
  height:22px;
  transform:translate(-50%,-50%);
  z-index:15;
  pointer-events:none;
  opacity:.95;
}
#crosshair:before,#crosshair:after{
  content:"";
  position:absolute;
  background:#fff;
  box-shadow:0 0 10px rgba(77,247,255,.8), 0 0 16px rgba(255,79,216,.5);
}
#crosshair:before{ left:10px; top:0; width:2px; height:22px; }
#crosshair:after{ left:0; top:10px; width:22px; height:2px; }

#damageFlash{
  position:fixed;
  inset:0;
  z-index:18;
  pointer-events:none;
  opacity:0;
  transition:opacity .15s ease;
  background:radial-gradient(circle, rgba(255,90,138,.08), rgba(255,0,120,.2));
}

#bossBarWrap{
  position:fixed;
  left:50%;
  transform:translateX(-50%);
  top:14px;
  width:min(620px, calc(100vw - 28px));
  z-index:21;
  display:none;
}
#bossBarWrap.show{ display:block; }
#bossBarLabel{
  text-align:center;
  margin-bottom:6px;
  font-size:12px;
  font-weight:800;
  letter-spacing:.08em;
  color:#ffe0ef;
}
#bossBar{
  width:100%;
  height:14px;
  border-radius:999px;
  overflow:hidden;
  background:rgba(255,255,255,.08);
  border:1px solid rgba(255,255,255,.12);
}
#bossBarInner{
  width:100%;
  height:100%;
  background:linear-gradient(90deg,#ff77a8,#ff2b80);
  box-shadow:0 0 18px rgba(255,43,128,.38);
}

#mailLink{
  position:fixed;
  right:10px;
  bottom:8px;
  z-index:26;
  font-size:11px;
  color:rgba(255,255,255,.55);
  text-decoration:none;
  background:rgba(0,0,0,.18);
  border:1px solid rgba(255,255,255,.08);
  border-radius:999px;
  padding:6px 10px;
  backdrop-filter:blur(6px);
  transition:.2s ease;
}

#minimapWrap{
  position:fixed;
  right:14px;
  top:14px;
  z-index:23;
  width:220px;
  height:220px;
  border-radius:18px;
  overflow:hidden;
  background:rgba(2,10,25,.58);
  border:1px solid rgba(255,255,255,.12);
  box-shadow:0 12px 28px rgba(0,0,0,.28);
  backdrop-filter:blur(10px);
}
#minimapLabel{
  position:absolute;
  left:10px;
  top:8px;
  z-index:2;
  font-size:10px;
  letter-spacing:.08em;
  text-transform:uppercase;
  color:rgba(255,255,255,.75);
}
#minimapCanvas{
  width:100%;
  height:100%;
  display:block;
}
#rightControls{
  display:flex;
  gap:14px;
  align-items:flex-end;
}
.joy-wrap{
  display:flex;
  flex-direction:column;
  align-items:center;
  gap:8px;
}
.joy-label{
  font-size:10px;
  font-weight:800;
  letter-spacing:.14em;
  text-transform:uppercase;
  color:rgba(255,255,255,.78);
  text-shadow:0 2px 8px rgba(0,0,0,.35);
}
#mailLink:hover{
  color:#fff;
  background:rgba(0,0,0,.32);
}

#mobileControls{
  position:fixed;
  inset:auto 0 12px 0;
  z-index:25;
  display:flex;
  justify-content:space-between;
  align-items:flex-end;
  padding:0 max(14px, env(safe-area-inset-left)) max(10px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-right));
  pointer-events:none;
}

#leftControls{
  display:flex;
  gap:14px;
  align-items:flex-end;
}

.joy{
  position:relative;
  width:126px;
  height:126px;
  border-radius:50%;
  background:radial-gradient(circle at 50% 50%, rgba(255,255,255,.08), rgba(255,255,255,.04));
  border:2px solid rgba(255,255,255,.16);
  pointer-events:auto;
  box-shadow:inset 0 0 26px rgba(255,255,255,.04), 0 10px 22px rgba(0,0,0,.25);
}
.look-joy{
  background:radial-gradient(circle at 50% 50%, rgba(255,180,89,.10), rgba(255,255,255,.03));
  border-color:rgba(255,183,76,.22);
}
#joyKnob,
#lookJoyKnob{
  position:absolute;
  width:52px;
  height:52px;
  left:37px;
  top:37px;
  border-radius:50%;
  background:linear-gradient(180deg, rgba(77,247,255,.95), rgba(157,107,255,.8));
  box-shadow:0 0 18px rgba(77,247,255,.45);
}
#lookJoyKnob{
  background:linear-gradient(180deg, rgba(255,215,110,.96), rgba(255,120,80,.82));
  box-shadow:0 0 18px rgba(255,181,72,.42);
}

#tapHint{
  position:fixed;
  right:14px;
  bottom:112px;
  z-index:25;
  padding:8px 10px;
  font-size:11px;
  border-radius:12px;
  background:rgba(0,0,0,.34);
  border:1px solid rgba(255,255,255,.08);
  color:rgba(255,255,255,.7);
  backdrop-filter:blur(8px);
  pointer-events:none;
}

@media (pointer:fine){
  #mobileControls, #tapHint{ display:none; }
}

@media (max-width:760px){
  #minimapWrap{
    top:auto;
    right:10px;
    bottom:148px;
    width:118px;
    height:118px;
    border-radius:16px;
  }
  #minimapLabel{ font-size:9px; }
}

@media (max-width:760px){
  #ui{
    left:50%;
    transform:translateX(-50%);
    top:8px;
    width:min(94vw, 390px);
    padding:8px 10px;
    border-radius:14px;
  }
  #brand{
    margin-bottom:6px;
  }
  #brandMark{
    width:30px;
    height:30px;
    font-size:12px;
  }
  #brandText b{ font-size:12px; }
  #brandText span{ font-size:10px; }
  #hud{
    grid-template-columns:repeat(4,minmax(0,1fr));
    gap:6px;
  }
  .stat{
    padding:6px 7px;
  }
  .stat .label{
    font-size:9px;
    margin-bottom:2px;
  }
  .stat .value{
    font-size:13px;
  }
  #msg{
    display:none;
  }
  #weaponBar{
    bottom:8px;
    gap:6px;
    max-width:94vw;
    overflow-x:auto;
    padding:0 6px 2px;
  }
  .weapon-chip{
    font-size:10px;
    padding:7px 9px;
    white-space:nowrap;
  }
  #crosshair{
    display:none;
  }
}

@media (max-width:940px) and (orientation:landscape){
  #ui{
    left:max(8px, env(safe-area-inset-left));
    top:max(8px, env(safe-area-inset-top));
    transform:none;
    width:min(330px, 44vw);
    padding:8px 9px;
    border-radius:12px;
    background:rgba(5,10,18,.70);
  }
  #brand{
    margin-bottom:4px;
  }
  #brandText span,
  #msg{
    display:none;
  }
  #hud{
    grid-template-columns:repeat(2,minmax(0,1fr));
    gap:5px;
  }
  .stat{
    padding:5px 6px;
  }
  .stat .label{
    font-size:8px;
    margin-bottom:1px;
  }
  .stat .value{
    font-size:12px;
  }
  #weaponBar{
    left:52%;
    bottom:max(6px, env(safe-area-inset-bottom));
    max-width:58vw;
    gap:5px;
  }
  .weapon-chip{
    font-size:10px;
    padding:6px 8px;
  }
  #abilityDock{
    right:max(10px, env(safe-area-inset-right));
    bottom:102px;
    gap:7px;
  }
  .ability-btn{
    min-width:66px;
    padding:8px 10px;
    border-radius:14px;
    font-size:11px;
  }
  .ability-btn small{
    font-size:9px;
    margin-top:3px;
  }
  #mobileControls{
    bottom:max(10px, env(safe-area-inset-bottom));
    left:max(10px, env(safe-area-inset-left));
    right:max(10px, env(safe-area-inset-right));
  }
  #moveJoy, #lookJoy{
    width:100px;
    height:100px;
  }
  #moveJoyKnob, #lookJoyKnob{
    width:46px;
    height:46px;
    left:27px;
    top:27px;
  }
  #tapHint{
    right:max(10px, env(safe-area-inset-right));
    bottom:88px;
    font-size:10px;
    padding:6px 8px;
  }
  #minimapWrap{
    right:max(10px, env(safe-area-inset-right));
    bottom:206px;
    width:104px;
    height:104px;
  }
}

#abilityDock{
  position:fixed;
  right:max(16px, env(safe-area-inset-right));
  bottom:160px;
  z-index:26;
  display:none;
  flex-direction:column;
  gap:10px;
  pointer-events:none;
}
.ability-btn{
  min-width:72px;
  border:1px solid rgba(255,255,255,.16);
  border-radius:16px;
  padding:9px 12px;
  color:#fff;
  font-weight:800;
  font-size:12px;
  line-height:1.15;
  letter-spacing:.02em;
  background:linear-gradient(180deg, rgba(15,25,45,.88), rgba(5,10,18,.82));
  backdrop-filter:blur(10px);
  box-shadow:0 10px 22px rgba(0,0,0,.28);
  pointer-events:auto;
  touch-action:manipulation;
}
.ability-btn small{
  display:block;
  margin-top:4px;
  color:rgba(255,255,255,.68);
  font-size:10px;
  font-weight:700;
}
.ability-btn.active{
  box-shadow:0 0 0 1px rgba(77,247,255,.7), 0 0 20px rgba(77,247,255,.24);
}
.ability-btn.empty{
  opacity:.45;
}
@media (pointer:coarse){
  #abilityDock{ display:flex; }
}
@media (pointer:coarse) and (orientation:landscape) and (max-height:560px){
  #ui{
    left:10px;
    transform:none;
    top:calc(8px + env(safe-area-inset-top));
    width:min(54vw, 340px);
    padding:8px;
  }
  #brandText span,
  #msg{
    display:none;
  }
  #hud{
    grid-template-columns:repeat(6,minmax(0,1fr));
    gap:4px;
  }
  .stat{
    padding:5px 6px;
    border-radius:10px;
  }
  .stat .label{ font-size:8px; }
  .stat .value{ font-size:11px; }
  #bossBarWrap{
    top:calc(8px + env(safe-area-inset-top));
    width:min(38vw, 280px);
  }
  #minimapWrap{
    top:calc(8px + env(safe-area-inset-top));
    right:10px;
    bottom:auto;
    width:92px;
    height:92px;
    border-radius:14px;
  }
  #minimapLabel{ display:none; }
  #weaponBar{
    left:50%;
    bottom:6px;
    transform:translateX(-50%) scale(.92);
    transform-origin:center bottom;
  }
  #mobileControls{
    inset:auto 0 6px 0;
    padding:0 max(10px, env(safe-area-inset-left)) max(6px, env(safe-area-inset-bottom)) max(10px, env(safe-area-inset-right));
  }
  .joy{
    width:108px;
    height:108px;
  }
  #joyKnob,
  #lookJoyKnob{
    width:44px;
    height:44px;
    left:32px;
    top:32px;
  }
  #tapHint{
    right:10px;
    bottom:88px;
    font-size:10px;
  }
  #abilityDock{
    right:max(10px, env(safe-area-inset-right));
    bottom:124px;
    gap:8px;
  }
  .ability-btn{
    min-width:64px;
    padding:8px 10px;
    border-radius:14px;
    font-size:11px;
  }
}
</style>
</head>

<body>
<div id="gameWrap"></div>
<div id="psyOverlay"></div>
<div id="damageFlash"></div>
<div id="crosshair"></div>

<div id="minimapWrap"><div id="minimapLabel">Tactische kaart</div><canvas id="minimapCanvas" width="220" height="220"></canvas></div>

<div id="bossBarWrap">
  <div id="bossBarLabel">BOSS</div>
  <div id="bossBar"><div id="bossBarInner"></div></div>
</div>

<div id="ui">
  <div id="brand">
    <div id="brandMark">OH</div>
    <div id="brandText">
      <b>Olde Hanter Arcade</b>
      <span>Downloadlink verlopen</span>
    </div>
  </div>

  <div id="hud">
    <div class="stat"><div class="label">Score</div><div class="value" id="score">0</div></div>
    <div class="stat"><div class="label">Wave</div><div class="value" id="wave">1</div></div>
    <div class="stat"><div class="label">HP</div><div class="value" id="hp">100</div></div>
    <div class="stat"><div class="label">Kills</div><div class="value" id="kills">0</div></div>
    <div class="stat"><div class="label">Bullets</div><div class="value" id="ammoBullets">50</div></div>
    <div class="stat"><div class="label">Rockets</div><div class="value" id="ammoRockets">0</div></div>
    <div class="stat"><div class="label">Grenades</div><div class="value" id="ammoGrenades">0</div></div>
    <div class="stat"><div class="label">Weapon</div><div class="value" id="weaponName">Bullet</div></div>
    <div class="stat"><div class="label">Plasma</div><div class="value" id="ammoPlasma">3</div></div>
    <div class="stat"><div class="label">Mines</div><div class="value" id="ammoMine">2</div></div>
    <div class="stat"><div class="label">Orbital</div><div class="value" id="ammoOrbital">1</div></div>
    <div class="stat"><div class="label">Combo</div><div class="value" id="combo">x1.0</div></div>
  </div>

  <div id="msg">Desktop: WASD / pijltjes / klik / 1-2-3 + 4-5-6 skills. Mobiel: linker joystick beweegt, rechter joystick kijkt, tik om te schieten en gebruik rechts de skillknoppen.</div>
</div>

<div id="weaponBar">
  <div class="weapon-chip active" id="chipBullet">1 Bullet</div>
  <div class="weapon-chip" id="chipRocket">2 Rocket</div>
  <div class="weapon-chip" id="chipGrenade">3 Grenade</div>
  <div class="weapon-chip" id="chipPlasma">4 Plasma Burst</div>
  <div class="weapon-chip" id="chipMine">5 Shock Mine</div>
  <div class="weapon-chip" id="chipOrbital">6 Orbital Beam</div>
</div>

<div id="centerMessage">
  <h1>Downloadlink verlopen</h1>
  <p>Speel ondertussen de vernieuwde arcade challenge met rijkere arena, professionelere effecten en een lokale leaderboard op dit apparaat.</p>

  <div id="nameRow">
    <input id="playerName" maxlength="18" placeholder="Jouw naam" value="Speler"/>
  </div>

  <p>Desktop: <b>WASD</b>, <b>klik</b>, <b>1/2/3</b> voor wapens en <b>4/5/6</b> voor skills. Mobiel: <b>linker joystick beweegt</b>, <b>rechter joystick kijkt</b>, <b>tik om te schieten</b> en gebruik de <b>skillknoppen rechts</b>.</p>

  <button id="startBtn">Start spel</button>
  <div><button id="restartBtn" style="display:none;">Opnieuw spelen</button></div>

  <div id="boardWrap">
    <div class="board-meta">
      <h3>Leaderboard</h3>
      <span>Lokaal op dit apparaat</span>
    </div>
    <ol id="leaderboard"></ol>
  </div>
</div>

<div id="mobileControls">
  <div id="leftControls">
    <div class="joy-wrap">
      <div class="joy-label">MOVE</div>
      <div class="joy" id="joy"><div id="joyKnob"></div></div>
    </div>
  </div>
  <div id="rightControls">
    <div class="joy-wrap">
      <div class="joy-label">LOOK</div>
      <div class="joy look-joy" id="lookJoy"><div id="lookJoyKnob"></div></div>
    </div>
  </div>
</div>

<div id="abilityDock">
  <button class="ability-btn" id="abilityPlasma">4 Plasma<small id="abilityPlasmaCount">3 charges</small></button>
  <button class="ability-btn" id="abilityMine">5 Mine<small id="abilityMineCount">2 charges</small></button>
  <button class="ability-btn" id="abilityOrbital">6 Orbital<small id="abilityOrbitalCount">1 charge</small></button>
</div>

<div id="tapHint">Rechter joystick kijkt · tik om te schieten</div>

<a id="mailLink" href="mailto:patrick@oldehanter.nl?subject=Nieuwe%20downloadlink%20aanvragen&body=Hallo%20Patrick,%0D%0A%0D%0ADe%20downloadlink%20is%20vervallen.%20Kun%20je%20een%20nieuwe%20sturen%3F%0D%0A%0D%0AMet%20vriendelijke%20groet,">Vervallen link? Vraag een nieuwe aan</a>

<script src="https://cdn.jsdelivr.net/npm/three@0.158/build/three.min.js"></script>

<script>
(() => {
  const isTouch = matchMedia("(pointer:coarse)").matches || "ontouchstart" in window;

  const ui = {
    score: document.getElementById("score"),
    wave: document.getElementById("wave"),
    hp: document.getElementById("hp"),
    kills: document.getElementById("kills"),
    ammoBullets: document.getElementById("ammoBullets"),
    ammoRockets: document.getElementById("ammoRockets"),
    ammoGrenades: document.getElementById("ammoGrenades"),
    ammoPlasma: document.getElementById("ammoPlasma"),
    ammoMine: document.getElementById("ammoMine"),
    ammoOrbital: document.getElementById("ammoOrbital"),
    combo: document.getElementById("combo"),
    weaponName: document.getElementById("weaponName"),
    chipBullet: document.getElementById("chipBullet"),
    chipRocket: document.getElementById("chipRocket"),
    chipGrenade: document.getElementById("chipGrenade"),
    chipPlasma: document.getElementById("chipPlasma"),
    chipMine: document.getElementById("chipMine"),
    chipOrbital: document.getElementById("chipOrbital"),
    center: document.getElementById("centerMessage"),
    startBtn: document.getElementById("startBtn"),
    restartBtn: document.getElementById("restartBtn"),
    damageFlash: document.getElementById("damageFlash"),
    bossBarWrap: document.getElementById("bossBarWrap"),
    bossBarInner: document.getElementById("bossBarInner"),
    leaderboard: document.getElementById("leaderboard"),
    playerName: document.getElementById("playerName"),
    gameWrap: document.getElementById("gameWrap"),
    minimapCanvas: document.getElementById("minimapCanvas"),
    minimapWrap: document.getElementById("minimapWrap"),
    abilityPlasma: document.getElementById("abilityPlasma"),
    abilityMine: document.getElementById("abilityMine"),
    abilityOrbital: document.getElementById("abilityOrbital"),
    abilityPlasmaCount: document.getElementById("abilityPlasmaCount"),
    abilityMineCount: document.getElementById("abilityMineCount"),
    abilityOrbitalCount: document.getElementById("abilityOrbitalCount")
  };

  const LB_KEY = "olde_hanter_arcade_leaderboard_v3";

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, m => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    })[m]);
  }

  function getPlayerName(){
    return (ui.playerName.value || "Speler").trim().slice(0,18) || "Speler";
  }

  function loadBoard(){
    try{ return JSON.parse(localStorage.getItem(LB_KEY) || "[]"); }
    catch(e){ return []; }
  }

  function saveBoard(rows){
    localStorage.setItem(LB_KEY, JSON.stringify(rows.slice(0,10)));
  }

  function renderBoard(){
    const rows = loadBoard();
    ui.leaderboard.innerHTML = rows.length
      ? rows.map(r => `<li><b>${escapeHtml(r.name)}</b> — ${r.score} punten — wave ${r.wave}</li>`).join("")
      : "<li>Nog geen scores</li>";
  }

  function submitScore(){
    const score = Math.floor(player.score);
    if(score <= 0) return;
    const rows = loadBoard();
    rows.push({
      name:getPlayerName(),
      score,
      wave:player.wave,
      ts:Date.now()
    });
    rows.sort((a,b) => b.score - a.score || b.wave - a.wave || a.ts - b.ts);
    saveBoard(rows);
    renderBoard();
  }

  renderBoard();

  const minimapCtx = ui.minimapCanvas.getContext("2d");

  let audioCtx = null;
  function ensureAudio(){
    if(!audioCtx){
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if(Ctx) audioCtx = new Ctx();
    }
    if(audioCtx && audioCtx.state === "suspended") audioCtx.resume();
  }

  function tone(freq=440, dur=0.06, type="square", volume=0.04, slide=0){
    if(!audioCtx) return;
    const now = audioCtx.currentTime;
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    const filter = audioCtx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.value = 2400;
    osc.type = type;
    osc.frequency.setValueAtTime(freq, now);
    if(slide) osc.frequency.linearRampToValueAtTime(Math.max(40, freq + slide), now + dur);
    gain.gain.setValueAtTime(volume, now);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + dur);
    osc.connect(filter);
    filter.connect(gain);
    gain.connect(audioCtx.destination);
    osc.start(now);
    osc.stop(now + dur);
  }

  function noiseBurst(dur=0.06, volume=0.02){
    if(!audioCtx) return;
    const size = Math.max(1, (audioCtx.sampleRate * dur)|0);
    const buffer = audioCtx.createBuffer(1, size, audioCtx.sampleRate);
    const data = buffer.getChannelData(0);
    for(let i=0;i<size;i++) data[i] = (Math.random()*2-1) * (1 - i / size);
    const src = audioCtx.createBufferSource();
    const gain = audioCtx.createGain();
    const filter = audioCtx.createBiquadFilter();
    filter.type = "bandpass";
    filter.frequency.value = 900;
    gain.gain.value = volume;
    src.buffer = buffer;
    src.connect(filter);
    filter.connect(gain);
    gain.connect(audioCtx.destination);
    src.start();
  }

  function sfxShoot(){ tone(930,0.05,"square",0.042,-280); }
  function sfxRocket(){ tone(180,0.13,"sawtooth",0.05,120); }
  function sfxGrenade(){ tone(320,0.1,"triangle",0.05,-140); }
  function sfxHit(){ tone(210,0.06,"sawtooth",0.04,-80); }
  function sfxEnemyDown(){ tone(260,0.07,"square",0.045,120); setTimeout(()=>tone(430,0.08,"triangle",0.03,-50),40); }
  function sfxDamage(){ noiseBurst(0.06,0.02); tone(130,0.08,"sawtooth",0.028,-50); }
  function sfxPickup(){ tone(540,0.07,"triangle",0.04,130); setTimeout(()=>tone(760,0.1,"triangle",0.03,90),50); }
  function sfxBoss(){ tone(85,0.16,"sawtooth",0.055,15); }

  const scene = new THREE.Scene();
  scene.fog = new THREE.Fog(0x090014, 22, 120);

  const camera = new THREE.PerspectiveCamera(75, innerWidth / innerHeight, 0.1, 1000);
  camera.rotation.order = "YXZ";
  camera.position.set(0, 1.7, 0);

  let lookYaw = 0;
  let lookPitch = 0;
  function applyCameraLook(){
    camera.rotation.y = lookYaw;
    camera.rotation.x = lookPitch;
  }

  const renderer = new THREE.WebGLRenderer({ antialias:true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setSize(innerWidth, innerHeight);
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  ui.gameWrap.appendChild(renderer.domElement);

  const hemi = new THREE.HemisphereLight(0xe1f3ff, 0x210034, 1.1);
  scene.add(hemi);

  const sun = new THREE.DirectionalLight(0xffffff, 1.1);
  sun.position.set(8,16,6);
  sun.castShadow = true;
  sun.shadow.mapSize.width = 1024;
  sun.shadow.mapSize.height = 1024;
  scene.add(sun);

  const neonA = new THREE.PointLight(0x4df7ff, 1.2, 35, 2);
  const neonB = new THREE.PointLight(0xff4fd8, 1.2, 35, 2);
  neonA.position.set(10,4,0);
  neonB.position.set(-10,4,0);
  scene.add(neonA, neonB);

  const floor = new THREE.Mesh(
    new THREE.PlaneGeometry(170,170,24,24),
    new THREE.MeshStandardMaterial({
      color:0x10182f,
      emissive:0x09111f,
      emissiveIntensity:0.22,
      roughness:0.96,
      metalness:0.08
    })
  );
  floor.rotation.x = -Math.PI/2;
  floor.receiveShadow = true;
  scene.add(floor);

  const grid = new THREE.GridHelper(160, 80, 0x4df7ff, 0x7d33ff);
  grid.position.y = 0.03;
  grid.material.transparent = true;
  grid.material.opacity = 0.10;
  scene.add(grid);

  const stars = new THREE.Group();
  for(let i=0;i<180;i++){
    const star = new THREE.Mesh(
      new THREE.SphereGeometry(0.05, 6, 6),
      new THREE.MeshBasicMaterial({
        color: [0x4df7ff, 0xff4fd8, 0x9dff7c, 0xffffff][i % 4]
      })
    );
    star.position.set((Math.random()-0.5)*150, Math.random()*42+8, (Math.random()-0.5)*150);
    stars.add(star);
  }
  scene.add(stars);

  const colliders = [];

  function addBox(w,h,d,x,y,z,color=0x243d84){
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(w,h,d),
      new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: 0.12,
        roughness:0.75,
        metalness:0.15
      })
    );
    mesh.position.set(x,y,z);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    scene.add(mesh);
    colliders.push({ mesh, box:new THREE.Box3().setFromObject(mesh) });
    return mesh;
  }

  function addCylinderCollider(radius,height,x,y,z,color=0x1d2c4f){
    const mesh = new THREE.Mesh(
      new THREE.CylinderGeometry(radius,radius,height,12),
      new THREE.MeshStandardMaterial({
        color,
        emissive:0x0d1a33,
        emissiveIntensity:0.35,
        roughness:0.75,
        metalness:0.18
      })
    );
    mesh.position.set(x,y,z);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    scene.add(mesh);
    colliders.push({ mesh, box:new THREE.Box3().setFromObject(mesh) });
    return mesh;
  }

  function buildArena(){
    const B = 62;
    addBox(2,5,B*2, -B,2.5,0, 0x19336c);
    addBox(2,5,B*2,  B,2.5,0, 0x19336c);
    addBox(B*2,5,2, 0,2.5,-B, 0x4b1f7c);
    addBox(B*2,5,2, 0,2.5, B, 0x4b1f7c);

    addBox(14,4,3, 0,2,-8, 0x2fb8ff);
    addBox(14,4,3, 0,2, 14, 0xff4fd8);

    addBox(3,4,18, -22,2,-6, 0x2c4df0);
    addBox(3,4,18, -34,2, 12, 0x8b4dff);
    addBox(12,4,3, -28,2, 22, 0x16c7b8);

    addBox(3,4,18, 22,2,-6, 0xff4fd8);
    addBox(3,4,18, 34,2, 12, 0x2fb8ff);
    addBox(12,4,3, 28,2, 22, 0x16c7b8);

    addBox(6,2.5,6, -10,1.25,16, 0x2c4df0);
    addBox(6,2.5,6, 10,1.25,16, 0xff4fd8);
    addBox(8,3,5, 0,1.5,28, 0x7d33ff);

    addBox(10,4,4, -18,2,-28, 0x2fb8ff);
    addBox(10,4,4,  18,2,-28, 0xff4fd8);
    addBox(8,4,8, 0,2,-38, 0x16c7b8);

    [
      [-40,1,-14], [-32,1,-14], [-24,1,-14],
      [24,1,-14], [32,1,-14], [40,1,-14],
      [-18,1,34], [-6,1,34], [6,1,34], [18,1,34]
    ].forEach(([x,y,z]) => addBox(4,2,2, x,y,z, 0xffd166));

    for(let i=0;i<10;i++){
      const x = (i < 5 ? -1 : 1) * (18 + (i%5)*8);
      const z = i < 5 ? 4 : -4;
      addCylinderCollider(0.9,5,x,2.5,z,0x18284e);
    }

    for(let i=0;i<8;i++){
      const angle = i / 8 * Math.PI * 2;
      const x = Math.cos(angle) * 44;
      const z = Math.sin(angle) * 44;

      const pole = new THREE.Mesh(
        new THREE.CylinderGeometry(0.18,0.24,6,8),
        new THREE.MeshStandardMaterial({ color:0x3a3f52, metalness:0.55, roughness:0.45 })
      );
      pole.position.set(x,3,z);
      pole.castShadow = true;
      scene.add(pole);

      const lamp = new THREE.Mesh(
        new THREE.BoxGeometry(0.8,0.35,0.8),
        new THREE.MeshStandardMaterial({
          color:i % 2 ? 0x4df7ff : 0xff4fd8,
          emissive:i % 2 ? 0x4df7ff : 0xff4fd8,
          emissiveIntensity:1.2
        })
      );
      lamp.position.set(x,5.8,z);
      scene.add(lamp);

      const glow = new THREE.PointLight(i % 2 ? 0x4df7ff : 0xff4fd8, 1.1, 14, 2);
      glow.position.set(x,5.6,z);
      scene.add(glow);
    }

    for(let z=-48; z<=48; z+=12){
      for(let x=-48; x<=48; x+=12){
        if(Math.abs(x) < 8 && Math.abs(z) < 8) continue;
        const tile = new THREE.Mesh(
          new THREE.BoxGeometry(8,0.15,8),
          new THREE.MeshStandardMaterial({
            color: ((x+z)/12) % 2 === 0 ? 0x101c3d : 0x1a1136,
            emissive: ((x+z)/12) % 2 === 0 ? 0x0b1734 : 0x120a27,
            emissiveIntensity:0.18,
            roughness:0.88,
            metalness:0.08
          })
        );
        tile.position.set(x,0.08,z);
        tile.receiveShadow = true;
        scene.add(tile);
      }
    }

    [
      [-44,1.5,28, 8,3,6, 0x205e7a],
      [-44,1.5,36, 8,3,6, 0x6a2868],
      [44,1.5,28, 8,3,6, 0x205e7a],
      [44,1.5,36, 8,3,6, 0x6a2868]
    ].forEach(([x,y,z,w,h,d,c]) => addBox(w,h,d,x,y,z,c));

    for(let i=0;i<7;i++){
      const beam = new THREE.Mesh(
        new THREE.BoxGeometry(110, 0.35, 1.1),
        new THREE.MeshStandardMaterial({ color:0x2a2f45, metalness:0.35, roughness:0.55 })
      );
      beam.position.set(0, 6.5, -42 + i*14);
      beam.castShadow = true;
      scene.add(beam);
    }

    for(let i=0;i<6;i++){
      const beam = new THREE.Mesh(
        new THREE.BoxGeometry(1.1, 0.35, 110),
        new THREE.MeshStandardMaterial({ color:0x2a2f45, metalness:0.35, roughness:0.55 })
      );
      beam.position.set(-42 + i*16, 6.3, 0);
      beam.castShadow = true;
      scene.add(beam);
    }
  }
  buildArena();

  const moon = new THREE.Mesh(
    new THREE.SphereGeometry(5.2, 24, 24),
    new THREE.MeshBasicMaterial({ color:0xbfd7ff })
  );
  moon.position.set(-40, 32, -65);
  scene.add(moon);

  const moonGlow = new THREE.PointLight(0x8bbcff, 0.9, 180, 2);
  moonGlow.position.copy(moon.position);
  scene.add(moonGlow);

  const skyline = new THREE.Group();
  for(let i=0;i<26;i++){
    const h = 8 + Math.random()*18;
    const tower = new THREE.Mesh(
      new THREE.BoxGeometry(4 + Math.random()*4, h, 4 + Math.random()*4),
      new THREE.MeshStandardMaterial({
        color: i % 2 ? 0x131b38 : 0x1a1431,
        emissive: i % 2 ? 0x10224b : 0x2a0f3d,
        emissiveIntensity: 0.22,
        roughness: 0.82,
        metalness: 0.18
      })
    );
    tower.position.set(-72 + i*6, h/2, -72 - Math.random()*12);
    tower.receiveShadow = true;
    skyline.add(tower);
  }
  scene.add(skyline);

  const emberField = new THREE.Group();
  for(let i=0;i<50;i++){
    const ember = new THREE.Mesh(
      new THREE.SphereGeometry(0.08 + Math.random()*0.05, 6, 6),
      new THREE.MeshBasicMaterial({ color: [0x4df7ff,0xff4fd8,0x9dff7c,0xffd166][i%4] })
    );
    ember.position.set(rand(-52,52), rand(0.8,4.5), rand(-52,52));
    ember.userData.baseY = ember.position.y;
    ember.userData.speed = rand(0.4,1.2);
    emberField.add(ember);
  }
  scene.add(emberField);

  const arenaDeco = {
    searchlights:new THREE.Group(),
    fogWisps:new THREE.Group(),
    crystalClusters:new THREE.Group(),
    ruins:new THREE.Group(),
    cliffs:new THREE.Group(),
    monoliths:new THREE.Group(),
    skyBands:new THREE.Group(),
    floatingShards:new THREE.Group()
  };
  scene.add(arenaDeco.searchlights, arenaDeco.fogWisps, arenaDeco.crystalClusters, arenaDeco.ruins, arenaDeco.cliffs, arenaDeco.monoliths, arenaDeco.skyBands, arenaDeco.floatingShards);

  function addDecalRing(x,z,radius,color=0x2bc1ff){
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(radius*0.72, radius, 40),
      new THREE.MeshBasicMaterial({ color, transparent:true, opacity:0.18, side:THREE.DoubleSide })
    );
    ring.rotation.x = -Math.PI/2;
    ring.position.set(x,0.031,z);
    scene.add(ring);
    return ring;
  }

  function buildSetDressing(){
    const rockMat = new THREE.MeshStandardMaterial({ color:0x202635, roughness:0.96, metalness:0.04 });
    const ruinMat = new THREE.MeshStandardMaterial({ color:0x3a3f52, roughness:0.78, metalness:0.16, emissive:0x111827, emissiveIntensity:0.16 });
    const crystalMat = new THREE.MeshStandardMaterial({ color:0x7be6ff, emissive:0x2a92b6, emissiveIntensity:0.8, roughness:0.24, metalness:0.32 });

    for(let i=0;i<22;i++){
      const cliff = new THREE.Mesh(
        new THREE.ConeGeometry(5 + Math.random()*6, 10 + Math.random()*16, 6),
        rockMat
      );
      cliff.position.set(-88 + i*8.2, 4 + Math.random()*2, (i % 2 ? -93 : 93) + Math.random()*6 - 3);
      cliff.rotation.z = rand(-0.08,0.08);
      cliff.rotation.x = rand(-0.05,0.05);
      cliff.castShadow = cliff.receiveShadow = true;
      arenaDeco.cliffs.add(cliff);
    }

    [
      { x:0, z:-56, w:24, h:8.5 },
      { x:-54, z:0, w:7.5, h:8 },
      { x:54, z:0, w:7.5, h:8 },
      { x:0, z:56, w:18, h:7.2 }
    ].forEach((cfg, idx) => {
      const left = new THREE.Mesh(new THREE.BoxGeometry(2.2, cfg.h, 2.2), ruinMat);
      const right = left.clone();
      const top = new THREE.Mesh(new THREE.BoxGeometry(cfg.w, 1.4, 2.4), ruinMat);
      if(Math.abs(cfg.x) > Math.abs(cfg.z)){
        left.position.set(cfg.x, cfg.h/2, cfg.z - cfg.w/2 + 1.6);
        right.position.set(cfg.x, cfg.h/2, cfg.z + cfg.w/2 - 1.6);
        top.position.set(cfg.x, cfg.h - 0.7, cfg.z);
        top.rotation.y = Math.PI/2;
      } else {
        left.position.set(cfg.x - cfg.w/2 + 1.6, cfg.h/2, cfg.z);
        right.position.set(cfg.x + cfg.w/2 - 1.6, cfg.h/2, cfg.z);
        top.position.set(cfg.x, cfg.h - 0.7, cfg.z);
      }
      [left,right,top].forEach(part => {
        part.castShadow = part.receiveShadow = true;
        arenaDeco.ruins.add(part);
      });
      addDecalRing(cfg.x, cfg.z, idx === 0 ? 6.8 : 4.8, idx % 2 ? 0xff4fd8 : 0x4df7ff);
    });

    for(let i=0;i<14;i++){
      const crystal = new THREE.Mesh(new THREE.OctahedronGeometry(0.8 + Math.random()*0.65, 0), crystalMat.clone());
      crystal.material.color.offsetHSL((Math.random()-0.5)*0.08, 0, 0);
      crystal.position.set((i < 7 ? -1 : 1) * rand(20,57), 0.8 + Math.random()*0.8, rand(-48,48));
      crystal.scale.y = 1.3 + Math.random()*2.5;
      crystal.rotation.y = Math.random()*Math.PI;
      crystal.castShadow = crystal.receiveShadow = true;
      crystal.userData.baseY = crystal.position.y;
      crystal.userData.floatOffset = Math.random()*Math.PI*2;
      arenaDeco.crystalClusters.add(crystal);
    }

    const fogGeo = new THREE.SphereGeometry(1.4, 10, 10);
    for(let i=0;i<18;i++){
      const fog = new THREE.Mesh(
        fogGeo,
        new THREE.MeshBasicMaterial({ color: i % 2 ? 0x4df7ff : 0xff4fd8, transparent:true, opacity:0.06, depthWrite:false })
      );
      fog.position.set(rand(-56,56), rand(0.5,2.2), rand(-56,56));
      fog.scale.setScalar(rand(1.4,3.2));
      fog.userData.base = fog.position.clone();
      fog.userData.speed = rand(0.12,0.34);
      fog.userData.phase = Math.random()*Math.PI*2;
      arenaDeco.fogWisps.add(fog);
    }

    for(let i=0;i<4;i++){
      const tower = new THREE.Mesh(new THREE.CylinderGeometry(0.34,0.5,11,10), ruinMat);
      tower.position.set(i < 2 ? -58 : 58, 5.5, i % 2 ? 42 : -42);
      tower.castShadow = tower.receiveShadow = true;
      arenaDeco.searchlights.add(tower);

      const head = new THREE.Mesh(new THREE.BoxGeometry(0.9,0.55,1.1), new THREE.MeshStandardMaterial({ color:0x858ca8, metalness:0.48, roughness:0.32 }));
      head.position.set(tower.position.x, 11.2, tower.position.z);
      head.castShadow = true;
      head.userData.phase = i * Math.PI * 0.5;
      arenaDeco.searchlights.add(head);

      const glow = new THREE.SpotLight(i % 2 ? 0x4df7ff : 0xffd166, 1.4, 96, 0.28, 0.45, 1);
      glow.position.copy(head.position);
      glow.target.position.set(0,0,0);
      scene.add(glow, glow.target);
      head.userData.spot = glow;
      head.userData.baseY = head.position.y;
    }

    [
      [-18,-18,0x4df7ff], [18,-18,0xff4fd8], [-18,18,0xffd166], [18,18,0x8bf0ff],
      [-30,0,0x6db7ff], [30,0,0xff6ea1], [0,-30,0x4df7ff], [0,30,0xffd166]
    ].forEach(([x,z,color], i) => {
      const monolith = new THREE.Mesh(
        new THREE.BoxGeometry(2.4, 8 + (i%3)*1.2, 2.4),
        new THREE.MeshStandardMaterial({ color:0x1b2338, emissive:color, emissiveIntensity:0.12, roughness:0.46, metalness:0.44 })
      );
      monolith.position.set(x, monolith.geometry.parameters.height/2, z);
      monolith.castShadow = monolith.receiveShadow = true;
      arenaDeco.monoliths.add(monolith);
      addDecalRing(x, z, 2.4, color);
    });

    const bandGeo = new THREE.PlaneGeometry(96, 12, 1, 1);
    for(let i=0;i<5;i++){
      const band = new THREE.Mesh(
        bandGeo,
        new THREE.MeshBasicMaterial({ color:i%2?0x4df7ff:0xff4fd8, transparent:true, opacity:0.08, side:THREE.DoubleSide, depthWrite:false })
      );
      band.position.set(0, 18 + i*4.5, -42 - i*8);
      band.rotation.x = -0.38 + i*0.02;
      band.userData.phase = i * 1.3;
      band.userData.baseY = band.position.y;
      arenaDeco.skyBands.add(band);
    }

    for(let i=0;i<24;i++){
      const shard = new THREE.Mesh(
        new THREE.OctahedronGeometry(0.24 + Math.random()*0.34, 0),
        new THREE.MeshStandardMaterial({ color:i%2?0xa7f1ff:0xffa0d2, emissive:i%2?0x3ab4cd:0xc94d8e, emissiveIntensity:0.52, roughness:0.22, metalness:0.34 })
      );
      shard.position.set(rand(-46,46), rand(3.4,9.8), rand(-46,46));
      shard.userData.base = shard.position.clone();
      shard.userData.phase = Math.random()*Math.PI*2;
      shard.userData.spin = rand(0.4,1.2);
      shard.castShadow = true;
      arenaDeco.floatingShards.add(shard);
    }
  }
  buildSetDressing();

  const weaponRig = new THREE.Group();
  camera.add(weaponRig);
  scene.add(camera);

  function buildViewWeapon(){
    weaponRig.clear();

    const bodyMat = new THREE.MeshStandardMaterial({ color:0x30374c, metalness:0.72, roughness:0.28 });
    const trimMat = new THREE.MeshStandardMaterial({ color:0x7de7ff, emissive:0x1d7e91, emissiveIntensity:0.7, metalness:0.52, roughness:0.24 });
    const woodMat = new THREE.MeshStandardMaterial({ color:0x6d4a2e, roughness:0.72, metalness:0.08 });

    const root = new THREE.Group();
    root.position.set(0.36, -0.35, -0.68);
    root.rotation.set(-0.08, -0.18, -0.08);

    const receiver = new THREE.Mesh(new THREE.BoxGeometry(0.18,0.2,0.6), bodyMat);
    receiver.castShadow = true;
    root.add(receiver);

    const barrel = new THREE.Mesh(new THREE.CylinderGeometry(0.032,0.036,0.72,16), bodyMat);
    barrel.rotation.x = Math.PI/2;
    barrel.position.set(0.0, 0.025, -0.55);
    barrel.castShadow = true;
    root.add(barrel);

    const shroud = new THREE.Mesh(new THREE.BoxGeometry(0.14,0.14,0.42), trimMat);
    shroud.position.set(0,0.04,-0.28);
    shroud.castShadow = true;
    root.add(shroud);

    const weaponLogo = makeLogoGlyph(0.18);
    weaponLogo.position.set(0.058, 0.045, -0.1);
    weaponLogo.rotation.y = Math.PI * 0.5;
    root.add(weaponLogo);

    const grip = new THREE.Mesh(new THREE.BoxGeometry(0.11,0.24,0.12), woodMat);
    grip.position.set(0,-0.19,0.05);
    grip.rotation.x = 0.22;
    grip.castShadow = true;
    root.add(grip);

    const stock = new THREE.Mesh(new THREE.BoxGeometry(0.14,0.16,0.28), woodMat);
    stock.position.set(0,-0.03,0.32);
    stock.castShadow = true;
    root.add(stock);

    const sight = new THREE.Mesh(new THREE.BoxGeometry(0.05,0.05,0.15), trimMat);
    sight.position.set(0,0.12,-0.02);
    root.add(sight);

    const muzzleFlash = new THREE.PointLight(0xffd27a, 0, 5, 2);
    muzzleFlash.position.set(0, 0.03, -0.9);
    root.add(muzzleFlash);
    weaponRig.userData.flash = muzzleFlash;
    weaponRig.userData.root = root;

    weaponRig.add(root);
  }
  buildViewWeapon();

  function updateViewWeapon(dt){
    const root = weaponRig.userData.root;
    if(!root) return;
    const moving = Math.hypot(input.forward, input.strafe) > 0.01;
    state.walkTime += dt * (moving ? 8.5 : 3.2);
    state.viewKick = Math.max(0, state.viewKick - dt * 4.2);
    state.cameraShake = Math.max(0, state.cameraShake - dt * 2.7);
    const bobX = Math.sin(state.walkTime) * (moving ? 0.018 : 0.005);
    const bobY = Math.abs(Math.cos(state.walkTime * 1.1)) * (moving ? 0.016 : 0.004);
    root.position.x = 0.36 + bobX + state.viewKick * 0.06;
    root.position.y = -0.35 - bobY - state.viewKick * 0.08;
    root.position.z = -0.68 + state.viewKick * 0.12;
    root.rotation.x = -0.08 + state.viewKick * 0.11;
    root.rotation.y = -0.18 - state.viewKick * 0.14;
    root.rotation.z = -0.08 + bobX * 0.7;
    const flash = weaponRig.userData.flash;
    if(flash) flash.intensity = Math.max(0, flash.intensity - dt * 18);
    camera.position.x += (Math.random()-0.5) * state.cameraShake * 0.05;
    camera.position.y += (Math.random()-0.5) * state.cameraShake * 0.04;
  }

  function drawMinimap(){
    const c = ui.minimapCanvas;
    const ctx = minimapCtx;
    if(!c || !ctx) return;
    const w = c.width, h = c.height;
    const range = 36;
    ctx.clearRect(0,0,w,h);
    const bg = ctx.createLinearGradient(0,0,0,h);
    bg.addColorStop(0, "rgba(7,22,40,.96)");
    bg.addColorStop(1, "rgba(3,8,18,.98)");
    ctx.fillStyle = bg;
    ctx.fillRect(0,0,w,h);

    ctx.strokeStyle = "rgba(77,247,255,.12)";
    ctx.lineWidth = 1;
    for(let i=0;i<=8;i++){
      const p = (i/8)*w;
      ctx.beginPath(); ctx.moveTo(p,0); ctx.lineTo(p,h); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(0,p); ctx.lineTo(w,p); ctx.stroke();
    }

    function worldToMap(wx,wz){
      const dx = wx - player.pos.x;
      const dz = wz - player.pos.z;
      return {
        x: w/2 + (dx/range)*(w/2-14),
        y: h/2 + (dz/range)*(h/2-14)
      };
    }
    function plot(wx,wz,color,size=4,stroke=null){
      const p = worldToMap(wx,wz);
      if(p.x<8||p.x>w-8||p.y<8||p.y>h-8) return;
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(p.x,p.y,size,0,Math.PI*2); ctx.fill();
      if(stroke){ ctx.strokeStyle = stroke; ctx.lineWidth = 1.5; ctx.stroke(); }
    }

    for(const cinfo of colliders){
      const b = cinfo.box;
      const p = worldToMap((b.min.x+b.max.x)*0.5, (b.min.z+b.max.z)*0.5);
      const rw = Math.max(2, ((b.max.x-b.min.x)/range)*(w/2-14));
      const rh = Math.max(2, ((b.max.z-b.min.z)/range)*(h/2-14));
      ctx.fillStyle = "rgba(120,170,255,.16)";
      ctx.fillRect(p.x-rw, p.y-rh, rw*2, rh*2);
    }

    const coneRadius = Math.min(w,h)*0.28;
    ctx.save();
    ctx.translate(w/2,h/2);
    ctx.rotate(-lookYaw);
    ctx.fillStyle = "rgba(77,247,255,.08)";
    ctx.beginPath();
    ctx.moveTo(0,0);
    ctx.arc(0,0,coneRadius,-0.42,0.42);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    for(const e of state.enemies){
      const color = e.type === "elite" ? "#ff9f6e" : e.type === "logo" ? "#ffd166" : e.type === "tank" ? "#ff7f95" : e.type === "runner" ? "#9dff7c" : "#7ed8ff";
      plot(e.mesh.position.x, e.mesh.position.z, color, e.type === "tank" ? 5 : 4);
    }
    if(state.boss){
      plot(state.boss.mesh.position.x, state.boss.mesh.position.z, "#ff2b80", 7, "rgba(255,255,255,.75)");
    }
    for(const p of state.pickups){
      const pickupColor = p.kind === "heal" ? "#62ffb0" : p.kind === "shield" ? "#74a8ff" : p.kind === "rocket" ? "#ff7b7b" : p.kind === "grenade" ? "#9dff7c" : "#ffd166";
      plot(p.mesh.position.x, p.mesh.position.z, pickupColor, 2.6);
    }
    for(const hzd of state.hazards){
      plot(hzd.mesh.position.x, hzd.mesh.position.z, hzd.kind === "mine" ? "#a2f3ff" : "#ff5c5c", 3.2);
    }

    ctx.save();
    ctx.translate(w/2, h/2);
    ctx.rotate(-lookYaw);
    ctx.fillStyle = "#f5fbff";
    ctx.beginPath();
    ctx.moveTo(0,-13); ctx.lineTo(8,9); ctx.lineTo(0,5); ctx.lineTo(-8,9);
    ctx.closePath();
    ctx.fill();
    ctx.restore();

    ctx.fillStyle = "rgba(245,251,255,.85)";
    ctx.font = "bold 12px system-ui";
    ctx.fillText("N", w/2-4, 14);
    ctx.font = "bold 11px system-ui";
    ctx.fillText(`Wave ${player.wave}`, 10, h-30);
    ctx.fillText(`Enemies ${state.enemies.length + (state.boss?1:0)}`, 10, h-15);
    ctx.textAlign = "right";
    ctx.fillText(`HP ${Math.round(player.hp)}`, w-10, h-15);
    ctx.textAlign = "left";
    ctx.strokeStyle = "rgba(77,247,255,.55)";
    ctx.lineWidth = 2;
    ctx.strokeRect(1,1,w-2,h-2);
  }

  function queueNextWave(delay=1.2){
    if(state.nextWaveQueued || !player.alive) return;
    state.nextWaveQueued = true;
    setTimeout(() => {
      state.nextWaveQueued = false;
      if(!player.alive) return;
      if(state.running && state.enemies.length === 0 && !state.boss){
        player.wave += 1;
        player.hp = Math.min(player.maxHp, player.hp + 12);
        player.ammo.bullet += 6;
        state.lastClearStamp = performance.now();
        setStat();
        spawnWave();
      }
    }, delay * 1000);
  }

  const musicLead = [784, 988, 1175, 1568, 1175, 988, 880, 988, 784, 988, 1175, 1760, 1568, 1175, 988, 880, 698, 880, 988, 1319, 988, 880, 784, 880, 659, 784, 988, 1175, 988, 784, 698, 784];
  const musicHarmony = [392, 494, 587, 784, 587, 494, 440, 494, 392, 494, 587, 880, 784, 587, 494, 440, 349, 440, 494, 659, 494, 440, 392, 440, 330, 392, 494, 587, 494, 392, 349, 392];
  const musicBass = [98, 98, 123, 123, 110, 110, 123, 123, 98, 98, 123, 147, 131, 131, 123, 110, 87, 87, 110, 110, 98, 98, 110, 110, 82, 82, 98, 98, 87, 87, 98, 98];
  const musicArp = [1568, 1760, 1976, 1760, 1568, 1760, 1976, 2349, 1319, 1568, 1760, 1568, 1319, 1568, 1760, 2093, 1175, 1319, 1568, 1319, 1175, 1319, 1568, 1760, 1047, 1175, 1319, 1175, 1047, 1175, 1319, 1568];
  function playChip(freq, when, dur, type, gain, detune=0){
    if(!audioCtx) return;
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    const filter = audioCtx.createBiquadFilter();
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(type === 'square' ? 1800 : 2400, when);
    o.type = type;
    o.frequency.setValueAtTime(freq, when);
    if(detune) o.detune.setValueAtTime(detune, when);
    g.gain.setValueAtTime(0.0001, when);
    g.gain.exponentialRampToValueAtTime(gain, when + 0.01);
    g.gain.exponentialRampToValueAtTime(Math.max(0.0001, gain*0.55), when + dur*0.55);
    g.gain.exponentialRampToValueAtTime(0.0001, when + dur);
    o.connect(filter).connect(g).connect(audioCtx.destination);
    o.start(when);
    o.stop(when + dur + 0.03);
  }

  function updateMusic(){
    if(!audioCtx || !state.running) return;
    const t = audioCtx.currentTime;
    while(state.songClock < t + 0.28){
      state.songStep = (state.songStep + 1) % musicLead.length;
      const when = Math.max(t, state.songClock);
      const lead = musicLead[state.songStep];
      const harmony = musicHarmony[state.songStep];
      const bass = musicBass[state.songStep];
      const arp = musicArp[state.songStep];
      const accent = state.songStep % 8 === 0;
      playChip(lead, when, accent ? 0.22 : 0.16, accent ? 'sawtooth' : 'square', accent ? 0.026 : 0.02, accent ? -4 : 0);
      playChip(harmony, when + 0.02, 0.14, 'triangle', 0.010);
      playChip(bass, when, 0.18, 'square', 0.015, -8);
      if(state.songStep % 2 === 0) playChip(arp, when + 0.09, 0.08, 'square', 0.008, 6);
      if(state.songStep % 4 === 3) noiseBurst(0.025, 0.0045);
      state.songClock += 0.145;
    }
  }

  const raycaster = new THREE.Raycaster();

  const player = {
    pos: new THREE.Vector3(0,1.7,0),
    radius: 0.7,
    speed: 10.2,
    hp: 100,
    maxHp: 100,
    score: 0,
    wave: 1,
    kills: 0,
    fireCooldown: 0,
    damageCooldown: 0,
    alive: true,
    ammo: {
      bullet: 64,
      rocket: 4,
      grenade: 3
    },
    abilities: {
      plasma: 3,
      mine: 2,
      orbital: 1
    },
    weapon: "bullet"
  };

  const state = {
    running:false,
    pointerLocked:false,
    lastTime: performance.now(),
    enemies: [],
    boss: null,
    bullets: [],
    enemyBullets: [],
    particles: [],
    pickups: [],
    rings: [],
    flashes: [],
    fireHeld:false,
    nextWaveQueued:false,
    viewKick:0,
    walkTime:0,
    cameraShake:0,
    songClock:0,
    songStep:-1,
    ambientPulse:0,
    combo:1,
    comboTimer:0,
    comboBest:1,
    emergencyAmmoTimer:0,
    ammoHintTimer:0,
    lastClearStamp:performance.now(),
    ragdolls: [],
    hazards: [],
    firedAbility: "",
    abilityFlashTimers: { plasma:null, mine:null, orbital:null }
  };

  const input = {
    keyboard:{},
    forward:0,
    strafe:0,
    turn:0,
    lookX:0,
    lookY:0
  };

  function weaponLabel(w){
    return w === "bullet" ? "Bullet" : w === "rocket" ? "Rocket" : "Grenade";
  }

  function pulseAbilityUI(kind){
    const chip = kind === "plasma" ? ui.chipPlasma : kind === "mine" ? ui.chipMine : ui.chipOrbital;
    const btn = kind === "plasma" ? ui.abilityPlasma : kind === "mine" ? ui.abilityMine : ui.abilityOrbital;
    chip?.classList.add("fired");
    btn?.classList.add("active");
    if(state.abilityFlashTimers[kind]) clearTimeout(state.abilityFlashTimers[kind]);
    state.abilityFlashTimers[kind] = setTimeout(() => {
      chip?.classList.remove("fired");
      btn?.classList.remove("active");
      if(state.firedAbility === kind) state.firedAbility = "";
    }, 240);
  }

  function setWeapon(w){
    player.weapon = w;
    ui.weaponName.textContent = weaponLabel(w);
    ui.chipBullet.classList.toggle("active", w === "bullet");
    ui.chipRocket.classList.toggle("active", w === "rocket");
    ui.chipGrenade.classList.toggle("active", w === "grenade");
    ui.chipPlasma.classList.remove("active");
    ui.chipMine.classList.remove("active");
    ui.chipOrbital.classList.remove("active");
  }

  function setStat(){
    ui.score.textContent = Math.floor(player.score);
    ui.wave.textContent = player.wave;
    ui.hp.textContent = Math.max(0, Math.floor(player.hp));
    ui.kills.textContent = player.kills;
    ui.ammoBullets.textContent = player.ammo.bullet;
    ui.ammoRockets.textContent = player.ammo.rocket;
    ui.ammoGrenades.textContent = player.ammo.grenade;
    ui.ammoPlasma.textContent = player.abilities.plasma;
    ui.ammoMine.textContent = player.abilities.mine;
    ui.ammoOrbital.textContent = player.abilities.orbital;
    ui.combo.textContent = state.combo > 1 ? `x${state.combo.toFixed(1)}` : "x1.0";
    ui.weaponName.textContent = weaponLabel(player.weapon);
    ui.chipBullet.classList.toggle("active", player.weapon === "bullet");
    ui.chipRocket.classList.toggle("active", player.weapon === "rocket");
    ui.chipGrenade.classList.toggle("active", player.weapon === "grenade");
    ui.abilityPlasmaCount.textContent = `${player.abilities.plasma} charges`;
    ui.abilityMineCount.textContent = `${player.abilities.mine} charges`;
    ui.abilityOrbitalCount.textContent = `${player.abilities.orbital} charges`;
    ui.abilityPlasma.classList.toggle("empty", player.abilities.plasma <= 0);
    ui.abilityMine.classList.toggle("empty", player.abilities.mine <= 0);
    ui.abilityOrbital.classList.toggle("empty", player.abilities.orbital <= 0);
  }
  setStat();
  resetPlayerPosition();

  function clamp(v,min,max){ return Math.max(min, Math.min(max, v)); }
  function rand(a,b){ return a + Math.random()*(b-a); }
  function totalAmmo(){
    return player.ammo.bullet + player.ammo.rocket + player.ammo.grenade;
  }

  function flashHint(message, duration=1500){
    const box = document.getElementById("msg");
    if(!box) return;
    box.textContent = message;
    state.ammoHintTimer = performance.now() + duration;
  }

  function restoreDefaultHint(){
    const box = document.getElementById("msg");
    if(!box) return;
    box.textContent = isTouch
      ? "Mobiel: linker joystick beweegt, rechter joystick kijkt, tik om te schieten en gebruik 4-5-6 rechts voor skills."
      : "Desktop: WASD / pijltjes / klik / 1-2-3 voor wapens en 4-5-6 voor skills. Mobiel heeft extra skillknoppen rechts.";
  }

  function showFloating(message){
    flashHint(message, 1650);
  }

  function ensureUsableWeapon(){
    if(player.weapon === "bullet" && player.ammo.bullet > 0) return;
    if(player.weapon === "rocket" && player.ammo.rocket > 0) return;
    if(player.weapon === "grenade" && player.ammo.grenade > 0) return;
    if(player.ammo.bullet > 0) return setWeapon("bullet");
    if(player.ammo.rocket > 0) return setWeapon("rocket");
    if(player.ammo.grenade > 0) return setWeapon("grenade");
    setWeapon("bullet");
  }

  function makeLogoGlyph(scale=1){
    const group = new THREE.Group();
    const mat = new THREE.MeshStandardMaterial({
      color:0xf5fbff,
      emissive:0x8fbfff,
      emissiveIntensity:0.62,
      roughness:0.36,
      metalness:0.24
    });
    const depth = 0.05 * scale;
    const stroke = 0.08 * scale;
    const h = 0.42 * scale;
    const oW = 0.28 * scale;
    const gap = 0.28 * scale;

    function addBar(w, hh, x, y){
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, hh, depth), mat);
      mesh.position.set(x,y,0);
      group.add(mesh);
      return mesh;
    }

    addBar(stroke, h, -gap, 0);
    addBar(stroke, h, -gap + oW, 0);
    addBar(oW + stroke, stroke, -gap + oW * 0.5, h * 0.5 - stroke * 0.5);
    addBar(oW + stroke, stroke, -gap + oW * 0.5, -h * 0.5 + stroke * 0.5);

    addBar(stroke, h, gap - 0.1 * scale, 0);
    addBar(stroke, h, gap + 0.1 * scale, 0);
    addBar(0.20 * scale, stroke, gap, 0);

    return group;
  }

  function findSafePlayerSpawn(){
    const candidates = [
      new THREE.Vector3(0, 1.7, 0),
      new THREE.Vector3(0, 1.7, -2),
      new THREE.Vector3(0, 1.7, 2),
      new THREE.Vector3(-3, 1.7, 0),
      new THREE.Vector3(3, 1.7, 0)
    ];

    for(const p of candidates){
      if(!collidesAt(p.x, p.z, player.radius)) return p;
    }

    for(let ring=0; ring<10; ring++){
      const radius = ring * 1.8;
      for(let i=0; i<24; i++){
        const angle = (i / 24) * Math.PI * 2;
        const p = new THREE.Vector3(Math.cos(angle) * radius, 1.7, Math.sin(angle) * radius);
        if(!collidesAt(p.x, p.z, player.radius)) return p;
      }
    }

    return new THREE.Vector3(0, 1.7, 0);
  }

  function resetPlayerPosition(){
    const spawn = findSafePlayerSpawn();
    player.pos.copy(spawn);
    camera.position.copy(spawn);
    camera.position.y = 1.7;
  }

  function collidesAt(x,z,radius=player.radius){
    const minX = x - radius, maxX = x + radius, minZ = z - radius, maxZ = z + radius;
    for(const c of colliders){
      const b = c.box;
      if(maxX > b.min.x && minX < b.max.x && maxZ > b.min.z && minZ < b.max.z) return true;
    }
    return false;
  }

  function moveWithCollision(dx,dz){
    const nx = player.pos.x + dx;
    const nz = player.pos.z + dz;
    if(!collidesAt(nx, player.pos.z)) player.pos.x = nx;
    if(!collidesAt(player.pos.x, nz)) player.pos.z = nz;
  }

  function makeLogoEnemyMesh(isBoss=false){
    const group = new THREE.Group();
    const navy = new THREE.MeshStandardMaterial({ color:0x0d2f63, emissive:0x071b3a, emissiveIntensity:isBoss?0.8:0.32, roughness:.38, metalness:.26 });
    const sky = new THREE.MeshStandardMaterial({ color:0x9fd8ef, emissive:0x397b95, emissiveIntensity:isBoss?0.75:0.36, roughness:.26, metalness:.18 });
    const body = new THREE.Group();
    const leftLeg = new THREE.Mesh(new THREE.BoxGeometry(0.22, isBoss?2.8:2.05, 0.36), navy);
    leftLeg.position.set(-0.78, isBoss?1.4:1.02, 0);
    const rightLeg = leftLeg.clone();
    rightLeg.position.x = 0.78;
    const leftFoot = new THREE.Mesh(new THREE.BoxGeometry(0.52, 0.18, 0.46), navy);
    leftFoot.position.set(-0.78, 0.08, 0);
    const rightFoot = leftFoot.clone();
    rightFoot.position.x = 0.78;
    const bridge = new THREE.Mesh(new THREE.BoxGeometry(1.35, 0.26, 0.38), sky);
    bridge.position.set(0, isBoss?2.12:1.52, 0);
    const crownL = new THREE.Mesh(new THREE.BoxGeometry(0.22, isBoss?1.2:0.9, 0.34), navy);
    crownL.position.set(-0.78, isBoss?3.06:2.34, 0);
    const crownR = crownL.clone();
    crownR.position.x = 0.78;
    const ringOuter = new THREE.Mesh(new THREE.CylinderGeometry(isBoss?0.7:0.52, isBoss?0.7:0.52, 0.34, 28), navy);
    ringOuter.rotation.x = Math.PI/2;
    ringOuter.position.set(0, isBoss?2.82:2.18, 0.02);
    const ringInner = new THREE.Mesh(new THREE.CylinderGeometry(isBoss?0.33:0.24, isBoss?0.33:0.24, 0.4, 24), sky);
    ringInner.rotation.x = Math.PI/2;
    ringInner.position.set(0, isBoss?2.82:2.18, 0.03);
    [leftLeg,rightLeg,leftFoot,rightFoot,bridge,crownL,crownR,ringOuter,ringInner].forEach(m => { m.castShadow = m.receiveShadow = true; body.add(m); });
    const shoulderL = new THREE.Mesh(new THREE.BoxGeometry(0.26,0.9,0.28), navy);
    shoulderL.position.set(-1.08, isBoss?2.12:1.64, 0);
    const shoulderR = shoulderL.clone();
    shoulderR.position.x = 1.08;
    const armL = new THREE.Mesh(new THREE.BoxGeometry(0.22, isBoss?1.3:1.0, 0.24), sky);
    armL.position.set(-1.08, isBoss?1.38:1.05, 0);
    const armR = armL.clone();
    armR.position.x = 1.08;
    const core = makeLogoGlyph(isBoss?1.25:1.0);
    core.position.set(0, isBoss?1.62:1.18, 0.24);
    body.add(shoulderL, shoulderR, armL, armR, core);
    const aura = new THREE.PointLight(0x9fd8ef, isBoss?1.8:0.8, isBoss?9:5, 2);
    aura.position.set(0, isBoss?2.5:1.9, 1.1);
    group.add(body, aura);
    group.userData.parts = { body, armL, armR, legL:leftLeg, legR:rightLeg, logo:core, hatBadge:core, ringOuter, ringInner };
    return group;
  }

  function makeEnemyMesh(type="basic", isBoss=false){
    const palette = isBoss
      ? { suit:0x143461, bodysuit:0x0c1b30, cape:0xa30f33, trim:0xd5f3ff, glow:0x8deaff }
      : type === "elite"
        ? { suit:0x244f84, bodysuit:0x12243d, cape:0x7d0f2a, trim:0xffe1a0, glow:0xffd166 }
        : type === "runner"
          ? { suit:0x3f75ba, bodysuit:0x1a3154, cape:0x8a1838, trim:0xd9f4ff, glow:0xa7f1ff }
          : type === "tank"
            ? { suit:0x284668, bodysuit:0x1a2332, cape:0x631323, trim:0xffd98b, glow:0xffd166 }
            : { suit:0x315c96, bodysuit:0x1d304f, cape:0x7a1631, trim:0xe8f7ff, glow:0x9fd8ef };

    const scale = isBoss ? 1.94 : type === "tank" ? 1.16 : type === "elite" ? 1.08 : 1.02;
    const group = new THREE.Group();
    const suitMat = new THREE.MeshStandardMaterial({ color:palette.suit, emissive:palette.bodysuit, emissiveIntensity:isBoss?0.36:0.16, roughness:.56, metalness:.16 });
    const bodysuitMat = new THREE.MeshStandardMaterial({ color:palette.bodysuit, roughness:.84, metalness:.08 });
    const trimMat = new THREE.MeshStandardMaterial({ color:palette.trim, emissive:palette.trim, emissiveIntensity:.20, roughness:.34, metalness:.28 });
    const glowMat = new THREE.MeshStandardMaterial({ color:palette.glow, emissive:palette.glow, emissiveIntensity:.42, roughness:.24, metalness:.34 });
    const capeMat = new THREE.MeshStandardMaterial({ color:palette.cape, emissive:0x230811, emissiveIntensity:.16, roughness:.74, metalness:.04, side:THREE.DoubleSide });
    const skinMat = new THREE.MeshStandardMaterial({ color:0xddb08b, roughness:.88, metalness:.03 });

    const pelvis = new THREE.Mesh(new THREE.BoxGeometry(type==="tank"?1.02:0.78,0.38,0.34), bodysuitMat);
    pelvis.position.y = 0.96;

    const torso = new THREE.Mesh(new THREE.BoxGeometry(type==="tank"?1.16:0.88,1.10,0.46), suitMat);
    torso.position.y = 1.76;

    const chestPlate = new THREE.Mesh(new THREE.BoxGeometry(type==="tank"?0.98:0.72,0.78,0.18), trimMat);
    chestPlate.position.set(0,1.84,0.24);

    const waistBelt = new THREE.Mesh(new THREE.BoxGeometry(type==="tank"?1.02:0.84,0.12,0.20), glowMat);
    waistBelt.position.set(0,1.24,0.24);

    const chestLogo = makeLogoGlyph(type === "tank" ? 0.78 : 0.66);
    chestLogo.position.set(0,1.84,0.35);
    const backLogo = makeLogoGlyph(type === "tank" ? 0.66 : 0.56);
    backLogo.position.set(0,1.78,-0.30);
    backLogo.rotation.y = Math.PI;

    const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.12,0.12,0.18,12), skinMat);
    neck.position.y = 2.36;
    const head = new THREE.Mesh(new THREE.BoxGeometry(0.54,0.64,0.54), skinMat);
    head.position.y = 2.80;

    const hair = new THREE.Mesh(new THREE.BoxGeometry(0.56,0.18,0.56), bodysuitMat);
    hair.position.set(0,3.05,0.01);
    const mask = new THREE.Mesh(new THREE.BoxGeometry(0.5,0.18,0.08), glowMat);
    mask.position.set(0,2.84,0.28);
    const jawGuard = new THREE.Mesh(new THREE.BoxGeometry(0.44,0.14,0.08), trimMat);
    jawGuard.position.set(0,2.62,0.27);

    const shoulderL = new THREE.Mesh(new THREE.BoxGeometry(0.34,0.24,0.30), suitMat);
    const shoulderR = shoulderL.clone();
    shoulderL.position.set(-0.60,2.06,0.02);
    shoulderR.position.set(0.60,2.06,0.02);

    const upperArmL = new THREE.Mesh(new THREE.BoxGeometry(0.24,0.74,0.24), suitMat);
    const upperArmR = upperArmL.clone();
    upperArmL.position.set(-0.62,1.62,0.02);
    upperArmR.position.set(0.62,1.62,0.02);

    const foreArmL = new THREE.Mesh(new THREE.BoxGeometry(0.22,0.74,0.22), bodysuitMat);
    const foreArmR = foreArmL.clone();
    foreArmL.position.set(-0.62,0.90,0.05);
    foreArmR.position.set(0.62,0.90,0.05);

    const gauntletL = new THREE.Mesh(new THREE.BoxGeometry(0.24,0.20,0.24), glowMat);
    const gauntletR = gauntletL.clone();
    gauntletL.position.set(-0.62,0.56,0.06);
    gauntletR.position.set(0.62,0.56,0.06);

    const handL = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.16,0.16), skinMat);
    const handR = handL.clone();
    handL.position.set(-0.62,0.42,0.06);
    handR.position.set(0.62,0.42,0.06);

    const thighL = new THREE.Mesh(new THREE.BoxGeometry(0.26,0.84,0.26), bodysuitMat);
    const thighR = thighL.clone();
    thighL.position.set(-0.23,0.46,0.03);
    thighR.position.set(0.23,0.46,0.03);

    const kneeL = new THREE.Mesh(new THREE.BoxGeometry(0.24,0.14,0.28), glowMat);
    const kneeR = kneeL.clone();
    kneeL.position.set(-0.23,0.08,0.08);
    kneeR.position.set(0.23,0.08,0.08);

    const shinL = new THREE.Mesh(new THREE.BoxGeometry(0.22,0.76,0.24), suitMat);
    const shinR = shinL.clone();
    shinL.position.set(-0.23,-0.24,0.03);
    shinR.position.set(0.23,-0.24,0.03);

    const bootL = new THREE.Mesh(new THREE.BoxGeometry(0.30,0.20,0.46), bodysuitMat);
    const bootR = bootL.clone();
    bootL.position.set(-0.23,-0.72,0.10);
    bootR.position.set(0.23,-0.72,0.10);

    const cape = new THREE.Mesh(new THREE.PlaneGeometry(type==="tank"?1.18:0.92, type==="tank"?1.52:1.24, 1, 6), capeMat);
    cape.position.set(0,1.68,-0.28);
    cape.rotation.x = 0.08;

    const shoulderHolster = new THREE.Mesh(new THREE.BoxGeometry(0.18,0.34,0.08), trimMat);
    shoulderHolster.position.set(-0.28,1.72,0.28);
    const radio = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.22,0.1), bodysuitMat);
    radio.position.set(0.28,1.58,0.28);

    const gun = new THREE.Group();
    const rifleBody = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.14,0.78), bodysuitMat);
    const rifleBarrel = new THREE.Mesh(new THREE.CylinderGeometry(0.028,0.028,0.86,12), trimMat);
    rifleBarrel.rotation.x = Math.PI/2;
    rifleBarrel.position.set(0,0,-0.55);
    const rifleStock = new THREE.Mesh(new THREE.BoxGeometry(0.14,0.14,0.24), suitMat);
    rifleStock.position.set(0,-0.02,0.34);
    const rifleCore = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.08,0.12), glowMat);
    rifleCore.position.set(0.04,0.03,-0.08);
    gun.add(rifleBody, rifleBarrel, rifleStock, rifleCore);
    gun.position.set(0.20,1.34,0.34);
    gun.rotation.x = -0.22;
    gun.rotation.y = Math.PI/2;

    [pelvis, torso, chestPlate, waistBelt, chestLogo, backLogo, neck, head, hair, mask, jawGuard, shoulderL, shoulderR, upperArmL, upperArmR, foreArmL, foreArmR, gauntletL, gauntletR, handL, handR, thighL, thighR, kneeL, kneeR, shinL, shinR, bootL, bootR, cape, shoulderHolster, radio, gun].forEach(m => {
      m.castShadow = true;
      m.receiveShadow = true;
      group.add(m);
    });

    if(isBoss){
      const crest = new THREE.Mesh(new THREE.TorusGeometry(1.08,.08,12,40), glowMat);
      crest.rotation.x = Math.PI/2;
      crest.position.y = 3.56;
      group.add(crest);
    }

    group.userData.parts = { armL: upperArmL, armR: upperArmR, foreArmL, foreArmR, legL: thighL, legR: thighR, shinL, shinR, gun, logo: chestLogo, backLogo, hatBadge: mask, head, torso, pelvis, bootL, bootR, chest: chestPlate, cape, gauntletL, gauntletR };
    group.scale.setScalar(scale);
    return group;
  }

  function spawnEnemy(isBoss=false){
    let x=0,z=0,tries=0;
    while(tries < 50){
      x = rand(-48,48);
      z = rand(-48,48);
      const dx = x-player.pos.x, dz = z-player.pos.z;
      if(Math.sqrt(dx*dx + dz*dz) > 14 && !collidesAt(x,z,1.2)) break;
      tries++;
    }

    let type = "basic";
    if(!isBoss){
      const roll = Math.random();
      if(player.wave >= 7 && roll < .16) type = "elite";
      else if(player.wave >= 4 && roll < .38) type = "tank";
      else if(player.wave >= 2 && roll < .68) type = "runner";
    }

    const mesh = makeEnemyMesh(type, isBoss);
    mesh.position.set(x,0,z);
    scene.add(mesh);

    const baseHp = isBoss ? 230 + player.wave*28 :
      type === "runner" ? 16 + player.wave*3 :
      type === "tank" ? 42 + player.wave*6 :
      type === "elite" ? 52 + player.wave*7 :
      type === "logo" ? 32 + player.wave*5 :
      22 + player.wave*4;

    const ringGeo = new THREE.RingGeometry(isBoss ? 1.8 : 0.78, isBoss ? 2.02 : 0.92, 40);
    const ringMat = new THREE.MeshBasicMaterial({ color:isBoss ? 0xff8aa7 : (type === "elite" ? 0xffd166 : 0x7dd8ff), transparent:true, opacity:isBoss ? 0.34 : 0.20, side:THREE.DoubleSide });
    const groundRing = new THREE.Mesh(ringGeo, ringMat);
    groundRing.rotation.x = -Math.PI/2;
    groundRing.position.set(x, 0.03, z);
    scene.add(groundRing);

    const enemy = {
      type,
      isBoss,
      mesh,
      groundRing,
      hp: baseHp,
      maxHp: baseHp,
      speed: isBoss ? 2.9 :
        type === "runner" ? 5.3 + player.wave*.14 :
        type === "tank" ? 2.2 + player.wave*.06 :
        type === "elite" ? 3.9 + player.wave*.1 :
        3.25 + player.wave*.1,
      radius: isBoss ? 1.8 : (type === "tank" ? 1.2 : type === "elite" ? 1.08 : 1.0),
      fireCooldown: isBoss ? .95 : rand(.9,2.2),
      strafe: rand(-1,1),
      bob: rand(0,Math.PI*2)
    };

    if(isBoss){
      state.boss = enemy;
      ui.bossBarWrap.classList.add("show");
      showFloating("BOSS INBOUND", "boss");
      createShockwave(mesh.position.clone(), 0xff6ea1, 4.8);
      sfxBoss();
    } else {
      if(type === "elite") showFloating("Elite trooper incoming");
      state.enemies.push(enemy);
    }
  }

  function spawnWave(){
    state.lastClearStamp = performance.now();
    const count = Math.min(6 + player.wave * 2, 30);
    showFloating(`WAVE ${player.wave} · ${count} vijanden`);
    for(let i=0;i<count;i++) spawnEnemy(false);
    if(player.wave % 4 === 0){
      setTimeout(() => {
        if(state.running && player.alive && !state.boss){
          if(state.enemies.length > 16){
            const overflow = state.enemies.splice(16);
            for(const extra of overflow) scene.remove(extra.mesh);
            if(extra.groundRing) scene.remove(extra.groundRing);
          }
          spawnEnemy(true);
        }
      }, 900);
    }
    if(player.wave >= 5 && player.wave % 3 === 0){
      setTimeout(() => {
        if(state.running && player.alive) spawnEnemy(false);
      }, 1500);
    }
    setStat();
  }

  function createProjectile(pos, dir, config){
    const material = new THREE.MeshBasicMaterial({ color: config.color });
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(config.size, 10, 10), material);
    mesh.position.copy(pos);
    scene.add(mesh);
    return {
      mesh,
      vel: dir.clone().multiplyScalar(config.speed),
      life: config.life,
      maxLife: config.life,
      friendly: !!config.friendly,
      damage: config.damage,
      radius: config.radius || 0,
      type: config.type || "bullet",
      gravity: config.gravity || 0,
      explosionColor: config.explosionColor || config.color,
      trailColor: config.trailColor || config.color,
      trailTimer: 0,
      smoke: !!config.smoke,
      spin: rand(-12, 12)
    };
  }

  function createBurst(position, color=0x74a8ff, count=12, speed=4, opts={}){
    for(let i=0;i<count;i++){
      const mesh = new THREE.Mesh(
        new THREE.SphereGeometry(rand(opts.minSize || .04, opts.maxSize || .09), 6, 6),
        new THREE.MeshBasicMaterial({ color })
      );
      mesh.position.copy(position);
      scene.add(mesh);
      state.particles.push({
        mesh,
        vel:new THREE.Vector3(rand(-1,1), rand(opts.minUp || .2, opts.maxUp || 1.4), rand(-1,1)).normalize().multiplyScalar(rand(speed*.6, speed)),
        life:rand(opts.minLife || .2, opts.maxLife || .7),
        drag: opts.drag || 0.92,
        gravity: opts.gravity == null ? 5.3 : opts.gravity,
        shrink: opts.shrink || 0.98,
        rotate: rand(-8,8)
      });
    }
  }

  function createShockwave(position, color, radius){
    const mesh = new THREE.Mesh(
      new THREE.TorusGeometry(0.45, 0.06, 10, 28),
      new THREE.MeshBasicMaterial({ color, transparent:true, opacity:0.9 })
    );
    mesh.position.copy(position);
    mesh.rotation.x = Math.PI / 2;
    scene.add(mesh);
    state.rings.push({ mesh, life:0.45, maxLife:0.45, grow:radius * 1.55 });
  }

  function createFlash(position, color, intensity=2.8, distance=9, life=0.15){
    const light = new THREE.PointLight(color, intensity, distance, 2);
    light.position.copy(position);
    scene.add(light);
    state.flashes.push({ light, life, maxLife:life });
  }

  function explodeAt(position, radius, damage, color){
    createBurst(position, color, 24, 7, { minSize:.06, maxSize:.14, minLife:.35, maxLife:1.0, maxUp:1.8, drag:0.9, gravity:4.4, shrink:0.972 });
    createBurst(position, 0xffffff, 10, 5, { minSize:.03, maxSize:.08, minLife:.12, maxLife:.32, gravity:1.5, shrink:0.95 });
    createBurst(position, 0x1e2438, 16, 3.2, { minSize:.09, maxSize:.18, minLife:.55, maxLife:1.3, minUp:.05, maxUp:.65, drag:0.96, gravity:0.7, shrink:0.985 });
    createShockwave(position, color, radius);
    createFlash(position, color, 4.2, radius * 5.2, 0.22);

    for(let i=state.enemies.length-1;i>=0;i--){
      const e = state.enemies[i];
      const hitPos = e.mesh.position.clone();
      hitPos.y = 1.7;
      const d = hitPos.distanceTo(position);
      if(d < radius){
        e.hp -= damage * (1 - d / radius);
        if(e.hp <= 0){
          killEnemy(e);
          state.enemies.splice(i,1);
        }
      }
    }

    if(state.boss){
      const bp = state.boss.mesh.position.clone();
      bp.y = 2.2;
      const d = bp.distanceTo(position);
      if(d < radius){
        state.boss.hp -= damage * (1 - d / radius);
        updateBossBar();
        if(state.boss.hp <= 0){
          killEnemy(state.boss);
        }
      }
    }
  }

  function dropPickup(position){
    const r = Math.random();
    let kind = null;

    if(r < .22) kind = "ammo";
    else if(r < .30) kind = "rocket";
    else if(r < .38) kind = "grenade";
    else if(r < .46) kind = "heal";
    else if(r < .53) kind = "shield";

    if(!kind) return;

    const colors = {
      ammo: 0xffd166,
      rocket: 0xff7b7b,
      grenade: 0x9dff7c,
      heal: 0x62ffb0,
      shield: 0x74a8ff
    };

    const emissive = {
      ammo: 0x7a5600,
      rocket: 0x7a2222,
      grenade: 0x215f1b,
      heal: 0x14684d,
      shield: 0x183560
    };

    const mat = new THREE.MeshStandardMaterial({ color: colors[kind], emissive: emissive[kind], emissiveIntensity:.95, metalness:.22, roughness:.32 });
    const mesh = new THREE.Group();
    let core;
    if(kind === "ammo"){
      core = new THREE.Mesh(new THREE.CylinderGeometry(.12,.12,.72,12), mat);
      const c2 = core.clone(); c2.rotation.z = Math.PI/2;
      mesh.add(core,c2);
    }else if(kind === "rocket"){
      core = new THREE.Mesh(new THREE.CylinderGeometry(.12,.12,.8,14), mat);
      core.rotation.z = Math.PI/2;
      const nose = new THREE.Mesh(new THREE.ConeGeometry(.14,.26,12), mat); nose.rotation.z = -Math.PI/2; nose.position.x = .48;
      mesh.add(core,nose);
    }else if(kind === "grenade"){
      core = new THREE.Mesh(new THREE.SphereGeometry(.28,14,14), mat);
      const pin = new THREE.Mesh(new THREE.TorusGeometry(.12,.03,8,16), mat); pin.position.y = .32;
      mesh.add(core,pin);
    }else if(kind === "heal"){
      core = new THREE.Mesh(new THREE.BoxGeometry(.22,.62,.22), mat);
      const bar = new THREE.Mesh(new THREE.BoxGeometry(.62,.22,.22), mat);
      mesh.add(core,bar);
    }else if(kind === "shield"){
      core = new THREE.Mesh(new THREE.TorusGeometry(.34,.09,10,18), mat);
      const gem = new THREE.Mesh(new THREE.OctahedronGeometry(.14,0), mat); gem.position.z = .12;
      mesh.add(core,gem);
    }
    mesh.position.copy(position);
    mesh.position.y = .95;
    scene.add(mesh);
    state.pickups.push({ mesh, kind, life:12, spin: rand(1.2,2.4) });
  }

  function registerKill(points){
    player.kills += 1;
    state.comboTimer = 3.2;
    state.combo = clamp(state.combo + 0.2, 1, 3.6);
    state.comboBest = Math.max(state.comboBest, state.combo);
    player.score += Math.round(points * state.combo);
    setStat();
  }

  function touchShootAt(clientX, clientY){
    const rect = renderer.domElement.getBoundingClientRect();
    const x = ((clientX - rect.left) / rect.width) * 2 - 1;
    const y = -((clientY - rect.top) / rect.height) * 2 + 1;
    raycaster.setFromCamera({x, y}, camera);
    const dir = raycaster.ray.direction.clone().normalize();
    shootWithDirection(dir);
  }

  function shootWithDirection(dirOverride=null){
    if(!state.running || !player.alive) return false;
    if(player.fireCooldown > 0) return false;

    let weapon = player.weapon;

    if(weapon === "bullet" && player.ammo.bullet <= 0) ensureUsableWeapon();
    if(weapon === "rocket" && player.ammo.rocket <= 0) ensureUsableWeapon();
    if(weapon === "grenade" && player.ammo.grenade <= 0) ensureUsableWeapon();
    weapon = player.weapon;

    if(player.weapon === "bullet" && player.ammo.bullet <= 0) return false;
    if(player.weapon === "rocket" && player.ammo.rocket <= 0) return false;
    if(player.weapon === "grenade" && player.ammo.grenade <= 0) return false;

    ensureAudio();

    const dir = dirOverride ? dirOverride.clone().normalize() : new THREE.Vector3();
    if(!dirOverride){
      camera.getWorldDirection(dir);
      dir.normalize();
    }

    const start = player.pos.clone();
    start.y = 1.52;
    start.add(dir.clone().multiplyScalar(.9));

    state.viewKick = Math.min(1.2, state.viewKick + (weapon === 'bullet' ? 0.34 : weapon === 'rocket' ? 0.85 : 0.62));
    state.cameraShake = Math.min(1.5, state.cameraShake + (weapon === 'bullet' ? 0.12 : weapon === 'rocket' ? 0.45 : 0.26));
    if(weaponRig.userData.flash) weaponRig.userData.flash.intensity = weapon === 'bullet' ? 2.8 : 4.0;

    if(weapon === "bullet"){
      player.ammo.bullet -= 1;
      state.bullets.push(createProjectile(start, dir, {
        speed: 31,
        friendly: true,
        color: 0xffec7d,
        trailColor: 0xfff7bf,
        size: 0.12,
        life: 2.2,
        damage: 10,
        type: "bullet"
      }));
      player.fireCooldown = 0.18;
      sfxShoot();
    } else if(weapon === "rocket"){
      player.ammo.rocket -= 1;
      state.bullets.push(createProjectile(start, dir, {
        speed: 18,
        friendly: true,
        color: 0xff7b7b,
        trailColor: 0xffb0a3,
        smoke: true,
        size: 0.18,
        life: 2.6,
        damage: 28,
        radius: 4.2,
        type: "rocket",
        explosionColor: 0xff7b7b
      }));
      player.fireCooldown = 0.55;
      sfxRocket();
    } else if(weapon === "grenade"){
      player.ammo.grenade -= 1;
      state.bullets.push(createProjectile(start, dir, {
        speed: 14,
        friendly: true,
        color: 0x9dff7c,
        trailColor: 0xd8ffca,
        smoke: true,
        size: 0.16,
        life: 1.6,
        damage: 22,
        radius: 3.6,
        type: "grenade",
        gravity: 10,
        explosionColor: 0x9dff7c
      }));
      player.fireCooldown = 0.65;
      sfxGrenade();
    }

    setStat();
    return true;
  }

  function enemyShoot(enemy){
    const start = enemy.mesh.position.clone();
    start.y = enemy.isBoss ? 2.9 : 2.05;
    const target = player.pos.clone();
    target.y = 1.45;
    const dir = target.sub(start).normalize();

    let speed = enemy.isBoss ? 18 : 12;
    let color = enemy.isBoss ? 0xff6ea1 : 0x78d7ff;
    let damage = enemy.isBoss ? 16 : 11;
    if(enemy.type === "runner"){ speed = 14; damage = 9; }
    if(enemy.type === "tank"){ speed = 10; damage = 14; color = 0xffd166; }
    if(enemy.type === "elite"){ speed = 13; damage = 15; color = 0xffa86e; }

    const burstCount = enemy.isBoss ? 2 : (enemy.type === "runner" ? 1 : enemy.type === "elite" ? 2 : 1);
    for(let i=0;i<burstCount;i++){
      const shotDir = dir.clone();
      shotDir.x += (Math.random()-0.5) * (enemy.isBoss ? 0.04 : 0.025);
      shotDir.y += (Math.random()-0.5) * 0.02;
      shotDir.z += (Math.random()-0.5) * (enemy.isBoss ? 0.04 : 0.025);
      shotDir.normalize();
      state.enemyBullets.push(createProjectile(start.clone(), shotDir, {
        speed,
        friendly:false,
        color,
        size: enemy.isBoss ? .18 : .13,
        life: 3.2,
        damage,
        type:"enemy"
      }));
    }
  }

  function spawnRagdoll(enemy){
    const pieces = [];
    const constraints = [];
    const base = enemy.mesh.position.clone();
    const color = enemy.isBoss ? 0x315c96 : enemy.type === "elite" ? 0x274f86 : enemy.type === "tank" ? 0x29496f : 0x315c96;
    const dark = enemy.isBoss ? 0x0c203d : 0x1d304f;
    const capeColor = enemy.isBoss ? 0x8a0f2d : 0x70142a;
    const skin = 0xddb08b;
    const impulse = new THREE.Vector3((Math.random()-0.5)*5.8, 5.0 + Math.random()*2.0, (Math.random()-0.5)*5.8);
    const defs = [
      { name:"head", geo:new THREE.BoxGeometry(0.44,0.56,0.42), off:[0,2.54,0.02], c:skin, mass:0.8 },
      { name:"torso", geo:new THREE.BoxGeometry(0.84,0.92,0.42), off:[0,1.66,0], c:color, mass:1.45 },
      { name:"hips", geo:new THREE.BoxGeometry(0.72,0.32,0.32), off:[0,0.98,0], c:dark, mass:1.2 },
      { name:"armL", geo:new THREE.BoxGeometry(0.22,0.88,0.22), off:[-0.58,1.44,0.02], c:color, mass:0.72 },
      { name:"armR", geo:new THREE.BoxGeometry(0.22,0.88,0.22), off:[0.58,1.44,0.02], c:color, mass:0.72 },
      { name:"legL", geo:new THREE.BoxGeometry(0.24,1.02,0.24), off:[-0.22,0.18,0.02], c:dark, mass:0.92 },
      { name:"legR", geo:new THREE.BoxGeometry(0.24,1.02,0.24), off:[0.22,0.18,0.02], c:dark, mass:0.92 },
      { name:"footL", geo:new THREE.BoxGeometry(0.30,0.18,0.44), off:[-0.22,-0.46,0.10], c:dark, mass:0.55 },
      { name:"footR", geo:new THREE.BoxGeometry(0.30,0.18,0.44), off:[0.22,-0.46,0.10], c:dark, mass:0.55 },
      { name:"cape", geo:new THREE.BoxGeometry(0.72,0.92,0.06), off:[0,1.46,-0.24], c:capeColor, mass:0.38 }
    ];
    const idx = {};
    defs.forEach((def, index) => {
      idx[def.name] = index;
      const mesh = new THREE.Mesh(def.geo, new THREE.MeshStandardMaterial({ color:def.c, roughness:.72, metalness:.10 }));
      const pos = base.clone().add(new THREE.Vector3(...def.off));
      mesh.position.copy(pos);
      mesh.rotation.set(Math.random()*0.30, Math.random()*Math.PI, Math.random()*0.30);
      mesh.castShadow = mesh.receiveShadow = true;
      scene.add(mesh);
      pieces.push({
        mesh,
        mass:def.mass,
        pos,
        prev:pos.clone().sub(impulse.clone().multiplyScalar(0.015 / def.mass)),
        spin:new THREE.Vector3((Math.random()-0.5)*2.8,(Math.random()-0.5)*3.8,(Math.random()-0.5)*2.8),
        lift:def.name === "cape" ? 0.9 : 0.35 + Math.random()*0.45,
        radius:0.18 + (def.name.includes("torso") ? 0.18 : def.name.includes("leg") ? 0.10 : 0.08),
        drag:def.name === "cape" ? 0.985 : 0.972
      });
    });

    const link = (a, b, slack=1) => {
      const pa = pieces[idx[a]].pos;
      const pb = pieces[idx[b]].pos;
      constraints.push({ a:idx[a], b:idx[b], len:pa.distanceTo(pb) * slack });
    };
    link("head", "torso", 1.0);
    link("torso", "hips", 1.0);
    link("torso", "armL", 1.0);
    link("torso", "armR", 1.0);
    link("hips", "legL", 1.0);
    link("hips", "legR", 1.0);
    link("legL", "footL", 1.0);
    link("legR", "footR", 1.0);
    link("armL", "armR", 1.08);
    link("legL", "legR", 1.12);
    link("torso", "cape", 1.0);
    link("hips", "cape", 1.06);

    state.ragdolls.push({ pieces, constraints, life:6.6, fade:1.8 });
  }

  function deployShockMine(){
    if(!state.running || !player.alive || player.abilities.mine <= 0) return;
    player.abilities.mine -= 1;
    state.firedAbility = "mine";
    pulseAbilityUI("mine");
    const group = new THREE.Group();
    const body = new THREE.Mesh(new THREE.CylinderGeometry(0.34,0.42,0.12,18), new THREE.MeshStandardMaterial({ color:0x8bf0ff, emissive:0x2a8aa0, emissiveIntensity:0.9, metalness:0.35, roughness:0.22 }));
    const coil = new THREE.Mesh(new THREE.TorusGeometry(0.4,0.05,10,28), new THREE.MeshStandardMaterial({ color:0xd8fbff, emissive:0x8bf0ff, emissiveIntensity:1.1 }));
    coil.rotation.x = Math.PI/2;
    group.add(body, coil);
    group.position.set(player.pos.x, 0.16, player.pos.z);
    scene.add(group);
    const aura = new THREE.Mesh(new THREE.RingGeometry(0.8, 1.15, 40), new THREE.MeshBasicMaterial({ color:0x8bf0ff, transparent:true, opacity:.5, side:THREE.DoubleSide }));
    aura.rotation.x = -Math.PI/2;
    aura.position.copy(group.position);
    aura.position.y = 0.03;
    scene.add(aura);
    state.hazards.push({ kind:"mine", mesh:group, aura, life:20, radius:5.8, triggerRadius:2.7, damage:58, pulse:0, tick:0 });
    createShockwave(group.position.clone(), 0x8bf0ff, 1.6);
    flashHint("Shock Mine geplaatst");
    setStat();
  }

  function deployOrbital(){
    if(!state.running || !player.alive || player.abilities.orbital <= 0) return;
    player.abilities.orbital -= 1;
    state.firedAbility = "orbital";
    pulseAbilityUI("orbital");
    const dir = new THREE.Vector3();
    camera.getWorldDirection(dir);
    const pos = player.pos.clone().add(dir.setY(0).normalize().multiplyScalar(12));
    pos.x = clamp(pos.x, -54, 54);
    pos.z = clamp(pos.z, -54, 54);
    pos.y = 0.2;
    const marker = new THREE.Mesh(new THREE.RingGeometry(1.25,1.9,36), new THREE.MeshBasicMaterial({ color:0xff6ea1, transparent:true, opacity:.86, side:THREE.DoubleSide }));
    marker.rotation.x = -Math.PI/2;
    marker.position.copy(pos);
    const inner = new THREE.Mesh(new THREE.CircleGeometry(1.1, 28), new THREE.MeshBasicMaterial({ color:0xff6ea1, transparent:true, opacity:.18, side:THREE.DoubleSide }));
    inner.rotation.x = -Math.PI/2;
    inner.position.copy(pos);
    inner.position.y = 0.02;
    const beam = new THREE.Mesh(new THREE.CylinderGeometry(0.7,1.6,40,18,1,true), new THREE.MeshBasicMaterial({ color:0xff8cc0, transparent:true, opacity:.18, depthWrite:false }));
    beam.position.set(pos.x, 20, pos.z);
    scene.add(marker, inner, beam);
    state.hazards.push({ kind:"orbital", mesh:marker, inner, beam, life:1.6, radius:7.4, damage:82, pulse:0, strikes:0, tick:0 });
    flashHint("Orbital lock bevestigd");
    setStat();
  }

  function firePlasmaBurst(){
    if(!state.running || !player.alive || player.abilities.plasma <= 0) return;
    player.abilities.plasma -= 1;
    state.firedAbility = "plasma";
    pulseAbilityUI("plasma");
    const dir = new THREE.Vector3();
    camera.getWorldDirection(dir);
    for(let i=-2;i<=2;i++){
      const d = dir.clone();
      d.x += i * 0.03;
      d.y += Math.abs(i) * 0.005;
      d.normalize();
      const start = player.pos.clone();
      start.y = 1.52;
      start.add(d.clone().multiplyScalar(.9));
      state.bullets.push(createProjectile(start, d, {
        speed: 26,
        friendly: true,
        color: 0x8bf0ff,
        trailColor: 0xc9fbff,
        size: 0.16,
        life: 2.0,
        damage: 14,
        radius: 2.8,
        type: "plasma",
        explosionColor: 0x8bf0ff
      }));
    }
    state.cameraShake = Math.min(1.6, state.cameraShake + 0.25);
    createShockwave(player.pos.clone(), 0x8bf0ff, 1.6);
    setStat();
  }

  function applyDamage(amount){
    if(!player.alive) return;
    if(player.damageCooldown > 0) return;

    player.hp = Math.max(0, player.hp - amount);
    player.damageCooldown = 0.32;
    state.cameraShake = Math.min(1.8, state.cameraShake + 0.65);
    ui.damageFlash.style.opacity = "1";
    setTimeout(() => ui.damageFlash.style.opacity = "0", 90);
    sfxDamage();
    setStat();

    if(player.hp <= 0){
      player.alive = false;
      state.running = false;
      submitScore();
      ui.center.classList.remove("hidden");
      ui.center.querySelector("h1").textContent = "Game over";
      ui.center.querySelector("p").textContent = "Je score is opgeslagen in de lokale leaderboard.";
      ui.startBtn.style.display = "none";
      ui.restartBtn.style.display = "";
      if(document.pointerLockElement === renderer.domElement) document.exitPointerLock();
    }
  }

  function killEnemy(enemy){
    spawnRagdoll(enemy);
    scene.remove(enemy.mesh);
    if(enemy.groundRing) scene.remove(enemy.groundRing);
    createBurst(enemy.mesh.position.clone().add(new THREE.Vector3(0,1.8,0)), enemy.isBoss ? 0xff6ea1 : (enemy.type === "elite" ? 0xffa86e : enemy.type === "runner" ? 0x9dff7c : enemy.type === "tank" ? 0xffd166 : 0x74a8ff), enemy.isBoss ? 28 : 16, enemy.isBoss ? 8 : 5);

    if(enemy.isBoss){
      registerKill(150);
      state.boss = null;
      ui.bossBarWrap.classList.remove("show");
      dropPickup(enemy.mesh.position.clone());
      dropPickup(enemy.mesh.position.clone().add(new THREE.Vector3(1,0,0)));
      dropPickup(enemy.mesh.position.clone().add(new THREE.Vector3(-1,0,0)));
      sfxBoss();
      queueNextWave(1.1);
    } else {
      registerKill(enemy.type === "elite" ? 24 : enemy.type === "tank" ? 18 : enemy.type === "runner" ? 12 : 10);
      dropPickup(enemy.mesh.position.clone());
      if(state.enemies.length <= 1 && !state.boss) queueNextWave(1.0);
    }

    state.lastClearStamp = performance.now();
    sfxEnemyDown();
  }

  function updateBossBar(){
    if(state.boss){
      const pct = clamp(state.boss.hp / state.boss.maxHp, 0, 1);
      ui.bossBarInner.style.width = (pct * 100).toFixed(1) + "%";
    }
  }

  function restartGame(){
    for(const arr of [state.bullets, state.enemyBullets, state.particles, state.pickups, state.rings, state.hazards]){
      while(arr.length){
        const item = arr.pop();
        if(item.mesh) scene.remove(item.mesh);
        if(item.aura) scene.remove(item.aura);
        if(item.inner) scene.remove(item.inner);
        if(item.beam) scene.remove(item.beam);
      }
    }
    while(state.ragdolls.length){
      const rag = state.ragdolls.pop();
      for(const piece of rag.pieces) scene.remove(piece.mesh);
    }

    for(const e of state.enemies){ scene.remove(e.mesh); if(e.groundRing) scene.remove(e.groundRing); }
    state.enemies.length = 0;
    if(state.boss){
      scene.remove(state.boss.mesh);
      if(state.boss.groundRing) scene.remove(state.boss.groundRing);
      state.boss = null;
    }

    resetPlayerPosition();
    player.hp = 100;
    player.score = 0;
    player.wave = 1;
    player.kills = 0;
    player.fireCooldown = 0;
    player.damageCooldown = 0;
    player.alive = true;
    player.ammo.bullet = 64;
    player.ammo.rocket = 4;
    player.ammo.grenade = 3;
    player.abilities.plasma = 3;
    player.abilities.mine = 2;
    player.abilities.orbital = 1;
    state.firedAbility = "";
    Object.keys(state.abilityFlashTimers).forEach(key => {
      if(state.abilityFlashTimers[key]) clearTimeout(state.abilityFlashTimers[key]);
      state.abilityFlashTimers[key] = null;
    });
    [ui.chipPlasma, ui.chipMine, ui.chipOrbital, ui.abilityPlasma, ui.abilityMine, ui.abilityOrbital].forEach(el => el?.classList.remove("active", "fired"));
    state.combo = 1;
    state.comboTimer = 0;
    state.comboBest = 1;
    setWeapon("bullet");
    state.emergencyAmmoTimer = 0;
    state.ammoHintTimer = 0;
    while(state.flashes.length){
      const flash = state.flashes.pop();
      if(flash.light) scene.remove(flash.light);
    }
    restoreDefaultHint();

    state.running = true;
    state.fireHeld = false;
    state.nextWaveQueued = false;
    state.songClock = audioCtx ? audioCtx.currentTime + 0.05 : 0;
    state.songStep = -1;
    state.viewKick = 0;
    state.cameraShake = 0;
    state.lastClearStamp = performance.now();

    lookYaw = 0;
    lookPitch = 0;
    input.lookX = 0;
    input.lookY = 0;
    applyCameraLook();

    ui.center.classList.add("hidden");
    ui.startBtn.style.display = "";
    ui.restartBtn.style.display = "none";
    ui.bossBarWrap.classList.remove("show");
    setStat();
    spawnWave();
  }

  function tryAdvanceWave(){
    if(state.enemies.length === 0 && !state.boss){
      if(performance.now() - state.lastClearStamp > 850){
        queueNextWave(0.85);
      }
    } else {
      state.lastClearStamp = performance.now();
    }
  }

  function updateKeyboardAxes(){
    const k = input.keyboard;
    let forward = 0;
    let strafe = 0;
    let turn = 0;

    if(k["KeyW"] || k["ArrowUp"]) forward += 1;
    if(k["KeyS"] || k["ArrowDown"]) forward -= 1;
    if(k["KeyA"]) strafe -= 1;
    if(k["KeyD"]) strafe += 1;
    if(k["ArrowLeft"] || k["KeyQ"]) turn += 1;
    if(k["ArrowRight"] || k["KeyE"]) turn -= 1;

    input.forward = forward;
    input.strafe = strafe;
    input.turn = turn;
  }

  function updateMovement(dt){
    updateKeyboardAxes();

    lookYaw += (input.turn * 1.9 + input.lookX * 2.35) * dt;
    lookPitch = clamp(lookPitch - input.lookY * 1.8 * dt, -1.05, 1.05);
    applyCameraLook();

    const forward = input.forward;
    const strafe = input.strafe;
    const len = Math.hypot(forward, strafe) || 1;
    const f = forward / len;
    const s = strafe / len;

    const sin = Math.sin(lookYaw);
    const cos = Math.cos(lookYaw);
    const speed = player.speed * dt;

    const dx = (-sin * f + cos * s) * speed;
    const dz = (-cos * f - sin * s) * speed;

    moveWithCollision(dx, dz);

    camera.position.copy(player.pos);
    camera.position.y = 1.7 + Math.sin(performance.now()*0.014) * (forward || strafe ? 0.03 : 0.01);
    applyCameraLook();
    updateViewWeapon(dt);
  }

  function updateBullets(dt){
    for(let i=state.bullets.length-1;i>=0;i--){
      const b = state.bullets[i];
      b.mesh.position.addScaledVector(b.vel, dt);
      if(b.gravity){
        b.vel.y -= b.gravity * dt;
      }
      b.life -= dt;

      let remove = b.life <= 0;

      if(collidesAt(b.mesh.position.x, b.mesh.position.z, 0.14)){
        if(b.type === "rocket" || b.type === "grenade"){
          explodeAt(b.mesh.position.clone(), b.radius, b.damage, b.explosionColor);
        } else {
          createBurst(b.mesh.position, b.explosionColor, 6, 2.5);
        }
        remove = true;
      }

      if(b.mesh.position.y <= 0.2 && (b.type === "grenade" || b.type === "plasma")){
        explodeAt(b.mesh.position.clone(), b.radius, b.damage, b.explosionColor);
        remove = true;
      }

      for(let j=state.enemies.length-1;j>=0 && !remove;j--){
        const e = state.enemies[j];
        const hitPos = e.mesh.position.clone();
        hitPos.y = 1.9;
        if(b.mesh.position.distanceTo(hitPos) < e.radius){
          if(b.type === "rocket" || b.type === "grenade" || b.type === "plasma"){
            explodeAt(b.mesh.position.clone(), b.radius, b.damage, b.explosionColor);
          } else {
            e.hp -= b.damage;
            createBurst(b.mesh.position, 0xffec7d, 6, 3);
            sfxHit();
            if(e.hp <= 0){
              killEnemy(e);
              state.enemies.splice(j,1);
            }
          }
          remove = true;
        }
      }

      if(state.boss && !remove){
        const bp = state.boss.mesh.position.clone();
        bp.y = 2.5;
        if(b.mesh.position.distanceTo(bp) < state.boss.radius){
          if(b.type === "rocket" || b.type === "grenade" || b.type === "plasma"){
            explodeAt(b.mesh.position.clone(), b.radius, b.damage, b.explosionColor);
          } else {
            state.boss.hp -= b.damage;
            createBurst(b.mesh.position, 0xff88bb, 8, 3);
            sfxHit();
            updateBossBar();
            if(state.boss.hp <= 0){
              killEnemy(state.boss);
            }
          }
          remove = true;
        }
      }

      if(remove){
        scene.remove(b.mesh);
        state.bullets.splice(i,1);
      }
    }

    for(let i=state.enemyBullets.length-1;i>=0;i--){
      const b = state.enemyBullets[i];
      b.mesh.position.addScaledVector(b.vel, dt);
      b.life -= dt;

      let remove = b.life <= 0;
      if(collidesAt(b.mesh.position.x, b.mesh.position.z, 0.14)) remove = true;

      const playerHit = new THREE.Vector3(player.pos.x, 1.45, player.pos.z);
      if(b.mesh.position.distanceTo(playerHit) < 1.15){
        applyDamage(b.damage);
        createBurst(b.mesh.position, 0xff6ea1, 7, 2.8);
        remove = true;
      }

      if(remove){
        scene.remove(b.mesh);
        state.enemyBullets.splice(i,1);
      }
    }
  }

  function updateEnemies(dt){
    for(let i=state.enemies.length-1;i>=0;i--){
      const e = state.enemies[i];
      e.bob += dt * (e.type === "runner" ? 6.5 : 4.2);
      if(e.mesh.userData.parts){
        const swing = Math.sin(e.bob) * (e.type === "runner" ? 0.6 : e.type === "tank" ? 0.22 : e.type === "elite" ? 0.4 : 0.34);
        e.mesh.userData.parts.armL.rotation.x = swing * 0.52;
        e.mesh.userData.parts.armR.rotation.x = -swing * 0.48;
        e.mesh.userData.parts.legL.rotation.x = -swing;
        e.mesh.userData.parts.legR.rotation.x = swing;
        if(e.mesh.userData.parts.foreArmL) e.mesh.userData.parts.foreArmL.rotation.x = swing * 0.35;
        if(e.mesh.userData.parts.foreArmR) e.mesh.userData.parts.foreArmR.rotation.x = -swing * 0.35;
        e.mesh.userData.parts.gun.rotation.z = Math.sin(e.bob * 0.5) * 0.05;
        if(e.mesh.userData.parts.cape) e.mesh.userData.parts.cape.rotation.x = 0.08 + Math.abs(Math.sin(e.bob * 0.7)) * 0.08;
        e.mesh.userData.parts.hatBadge.rotation.z = Math.sin(e.bob * 0.4) * 0.03;
      }
      e.fireCooldown -= dt;

      const dx = player.pos.x - e.mesh.position.x;
      const dz = player.pos.z - e.mesh.position.z;
      const dist = Math.max(0.001, Math.hypot(dx, dz));
      const dirX = dx / dist;
      const dirZ = dz / dist;

      const ideal = e.type === "tank" ? (dist > 8 ? 1 : -0.12) : e.type === "elite" ? (dist > 10 ? 1 : -0.2) : (dist > 7 ? 1 : -0.32);
      const sideX = -dirZ * e.strafe * (e.type === "runner" ? 0.5 : 0.3);
      const sideZ =  dirX * e.strafe * (e.type === "runner" ? 0.5 : 0.3);

      const mx = (dirX * ideal + sideX * dt) * e.speed * dt;
      const mz = (dirZ * ideal + sideZ * dt) * e.speed * dt;

      const nx = e.mesh.position.x + mx;
      const nz = e.mesh.position.z + mz;
      if(!collidesAt(nx, nz, e.radius)){
        e.mesh.position.x = nx;
        e.mesh.position.z = nz;
      }

      e.mesh.position.y = 0.02 + Math.sin(e.bob) * 0.04;
      if(e.groundRing){
        e.groundRing.position.set(e.mesh.position.x, 0.03, e.mesh.position.z);
        e.groundRing.material.opacity = 0.12 + 0.16 * (e.hp / e.maxHp);
      }
      e.mesh.lookAt(player.pos.x, 1.6, player.pos.z);

      if(dist < (e.type === "tank" ? 2.0 : e.type === "elite" ? 1.85 : 1.6)){
        applyDamage((e.type === "tank" ? 18 : e.type === "elite" ? 17 : 12) * dt * 8);
        if(Math.random() < dt * (e.type === "runner" ? 3.0 : 2.0)) createShockwave(e.mesh.position.clone(), e.type === "elite" ? 0xffa86e : 0xff6ea1, e.type === "tank" ? 2.4 : 1.6);
      }

      if(e.type === "elite" && e.fireCooldown <= 0 && dist < 14){
        createShockwave(e.mesh.position.clone(), 0xffa86e, 3.6);
        explodeAt(player.pos.clone(), 3.0, 8, 0xffa86e);
        e.fireCooldown = rand(3.0,4.2);
      } else if(e.fireCooldown <= 0 && dist < (e.type === "tank" ? 18 : 24)){
        enemyShoot(e);
        e.fireCooldown = e.type === "runner" ? rand(1.1,1.8) : e.type === "tank" ? rand(1.7,2.6) : e.type === "elite" ? rand(1.5,2.1) : rand(0.95,1.8);
      }
    }

    if(state.boss){
      const e = state.boss;
      e.bob += dt * 2.2;
      if(e.mesh.userData.parts){
        const swing = Math.sin(e.bob) * 0.22;
        e.mesh.userData.parts.armL.rotation.x = swing * 0.5;
        e.mesh.userData.parts.armR.rotation.x = -swing * 0.5;
        e.mesh.userData.parts.legL.rotation.x = -swing * 0.75;
        e.mesh.userData.parts.legR.rotation.x = swing * 0.75;
        if(e.mesh.userData.parts.cape) e.mesh.userData.parts.cape.rotation.x = 0.1 + Math.abs(Math.sin(e.bob * 0.45)) * 0.1;
        e.mesh.userData.parts.hatBadge.rotation.z = Math.sin(e.bob * 0.35) * 0.04;
      }
      e.fireCooldown -= dt;

      const dx = player.pos.x - e.mesh.position.x;
      const dz = player.pos.z - e.mesh.position.z;
      const dist = Math.max(0.001, Math.hypot(dx, dz));
      const dirX = dx / dist;
      const dirZ = dz / dist;

      if(dist > 9){
        const nx = e.mesh.position.x + dirX * e.speed * dt;
        const nz = e.mesh.position.z + dirZ * e.speed * dt;
        if(!collidesAt(nx, nz, e.radius)){
          e.mesh.position.x = nx;
          e.mesh.position.z = nz;
        }
      }

      e.mesh.position.y = 0.04 + Math.sin(e.bob) * 0.06;
      if(e.groundRing){
        e.groundRing.position.set(e.mesh.position.x, 0.03, e.mesh.position.z);
        e.groundRing.material.opacity = 0.18 + 0.22 * (e.hp / e.maxHp);
      }
      e.mesh.lookAt(player.pos.x, 2.0, player.pos.z);

      if(dist < 2.6){
        applyDamage(22 * dt * 8);
      }

      if(e.fireCooldown <= 0 && dist < 32){
        enemyShoot(e);
        enemyShoot(e);
        if(player.wave >= 8) enemyShoot(e);
        e.fireCooldown = player.wave >= 8 ? 0.42 : 0.58;
      }

      updateBossBar();
    }
  }

  function updateParticles(dt){
    for(let i=state.particles.length-1;i>=0;i--){
      const p = state.particles[i];
      p.mesh.position.addScaledVector(p.vel, dt);
      p.vel.multiplyScalar(p.drag || 0.92);
      p.vel.y -= (p.gravity == null ? 5.3 : p.gravity) * dt;
      p.life -= dt;
      p.mesh.material.transparent = true;
      p.mesh.material.opacity = clamp(p.life * 1.8, 0, 1);
      if(p.shrink) p.mesh.scale.multiplyScalar(p.shrink);
      if(p.rotate) p.mesh.rotation.y += p.rotate * dt;
      if(p.life <= 0){
        scene.remove(p.mesh);
        state.particles.splice(i,1);
      }
    }
  }

  function updateRagdolls(dt){
    const gravity = 18;
    for(let i=state.ragdolls.length-1;i>=0;i--){
      const r = state.ragdolls[i];
      r.life -= dt;
      for(const piece of r.pieces){
        const velocity = piece.pos.clone().sub(piece.prev).multiplyScalar(0.988);
        piece.prev.copy(piece.pos);
        piece.pos.add(velocity);
        piece.pos.y += (piece.lift - gravity) * dt * dt;
      }

      for(let iter=0; iter<5; iter++){
        for(const c of r.constraints){
          const a = r.pieces[c.a];
          const b = r.pieces[c.b];
          const delta = b.pos.clone().sub(a.pos);
          let dist = delta.length() || 0.0001;
          const diff = (dist - c.len) / dist;
          const aWeight = b.mass / (a.mass + b.mass);
          const bWeight = a.mass / (a.mass + b.mass);
          a.pos.addScaledVector(delta, diff * 0.5 * aWeight);
          b.pos.addScaledVector(delta, -diff * 0.5 * bWeight);
        }

        for(const piece of r.pieces){
          const floorY = 0.08 + piece.radius;
          if(piece.pos.y < floorY){
            const vy = piece.pos.y - piece.prev.y;
            piece.pos.y = floorY;
            piece.prev.y = piece.pos.y + vy * -0.22;
            piece.prev.x = piece.pos.x - (piece.pos.x - piece.prev.x) * 0.72;
            piece.prev.z = piece.pos.z - (piece.pos.z - piece.prev.z) * 0.72;
          }
        }
      }

      const opacity = clamp(r.life / r.fade, 0, 1);
      for(const piece of r.pieces){
        const move = piece.pos.clone().sub(piece.prev);
        piece.mesh.position.copy(piece.pos);
        piece.mesh.rotation.x += piece.spin.x * dt + move.z * 0.6;
        piece.mesh.rotation.y += piece.spin.y * dt + move.x * 0.35;
        piece.mesh.rotation.z += piece.spin.z * dt - move.x * 0.6;
        piece.mesh.material.transparent = true;
        piece.mesh.material.opacity = opacity;
      }
      if(r.life <= 0){
        for(const piece of r.pieces) scene.remove(piece.mesh);
        state.ragdolls.splice(i,1);
      }
    }
  }

  function updateHazards(dt){
    for(let i=state.hazards.length-1;i>=0;i--){
      const h = state.hazards[i];
      h.life -= dt;
      h.pulse += dt;
      if(h.kind === "mine"){
        h.mesh.rotation.y += dt * 4;
        h.mesh.position.y = 0.16 + Math.sin(h.pulse * 5) * 0.03;
        if(h.aura){
          h.aura.position.copy(h.mesh.position);
          h.aura.position.y = 0.03;
          const auraPulse = 1 + Math.sin(h.pulse * 6) * 0.18;
          h.aura.scale.setScalar(auraPulse);
          h.aura.material.opacity = 0.22 + Math.sin(h.pulse * 8) * 0.12;
        }
        h.tick -= dt;
        if(h.tick <= 0){
          h.tick = 0.42;
          for(let j=state.enemies.length-1;j>=0;j--){
            const e = state.enemies[j];
            const dist = e.mesh.position.distanceTo(h.mesh.position);
            if(dist < h.radius){
              e.hp -= 10;
              const arcPos = e.mesh.position.clone().lerp(h.mesh.position, 0.45).add(new THREE.Vector3(0,1.2,0));
              createBurst(arcPos, 0x8bf0ff, 4, 2.2, { minLife:.12, maxLife:.24, gravity:0.2, shrink:0.92 });
              createFlash(arcPos, 0x8bf0ff, 1.5, 5, 0.08);
              if(dist < h.triggerRadius) h.life = Math.min(h.life, 0.08);
              if(e.hp <= 0){
                killEnemy(e);
                state.enemies.splice(j,1);
              }
            }
          }
        }
        if(h.life <= 0){
          explodeAt(h.mesh.position.clone(), h.radius, h.damage, 0x8bf0ff);
        }
      }else if(h.kind === "orbital"){
        const pulse = 0.45 + Math.sin(h.pulse * 16) * 0.18;
        h.mesh.material.opacity = 0.58 + Math.sin(h.pulse * 15) * 0.22;
        h.mesh.rotation.z += dt * 1.4;
        if(h.inner){
          h.inner.material.opacity = 0.15 + pulse * 0.22;
          h.inner.scale.setScalar(1 + Math.sin(h.pulse * 9) * 0.08);
        }
        if(h.beam){
          h.beam.material.opacity = 0.10 + pulse * 0.22;
          h.beam.position.y = 18 + Math.sin(h.pulse * 10) * 0.7;
        }
        h.tick -= dt;
        if(h.tick <= 0){
          h.tick = 0.2;
          h.strikes += 1;
          const strike = h.mesh.position.clone();
          strike.y = 0.4;
          createFlash(strike.clone().add(new THREE.Vector3(0,10,0)), 0xff8cc0, 2.2, 10, 0.12);
          createBurst(strike.clone().add(new THREE.Vector3(0,0.8,0)), 0xff8cc0, 10, 5.2, { minLife:.16, maxLife:.38, gravity:0.8, shrink:0.94 });
          if(h.strikes < 5){
            explodeAt(strike, Math.max(2.6, h.radius * 0.45), 14, 0xff8cc0);
          }
        }
        if(h.life <= 0){
          const strike = h.mesh.position.clone();
          strike.y = 0.4;
          for(let n=0;n<6;n++) createFlash(strike.clone(), 0xff6ea1, 7, 22, 0.2);
          if(h.beam){
            h.beam.material.opacity = 0.5;
            h.beam.scale.set(1.6, 1.0, 1.6);
          }
          createShockwave(strike, 0xff6ea1, h.radius);
          explodeAt(strike, h.radius, h.damage, 0xff6ea1);
        }
      }
      if(h.life <= 0){
        scene.remove(h.mesh);
        if(h.aura) scene.remove(h.aura);
        if(h.inner) scene.remove(h.inner);
        if(h.beam) scene.remove(h.beam);
        state.hazards.splice(i,1);
      }
    }
  }

  function updateEffects(dt){
    for(let i=state.rings.length-1;i>=0;i--){
      const ring = state.rings[i];
      ring.life -= dt;
      const t = 1 - clamp(ring.life / ring.maxLife, 0, 1);
      const scale = 1 + t * ring.grow;
      ring.mesh.scale.setScalar(scale);
      ring.mesh.material.opacity = 1 - t;
      ring.mesh.position.y += dt * 0.24;
      if(ring.life <= 0){
        scene.remove(ring.mesh);
        state.rings.splice(i,1);
      }
    }

    for(let i=state.flashes.length-1;i>=0;i--){
      const f = state.flashes[i];
      f.life -= dt;
      f.light.intensity = clamp(f.life / f.maxLife, 0, 1) * 4.0;
      if(f.life <= 0){
        scene.remove(f.light);
        state.flashes.splice(i,1);
      }
    }
  }

  function updatePickups(dt){
    for(let i=state.pickups.length-1;i>=0;i--){
      const p = state.pickups[i];
      p.life -= dt;
      p.mesh.rotation.x += dt * 1.2;
      p.mesh.rotation.y += dt * (p.spin || 2.1);
      p.mesh.position.y = 1 + Math.sin(performance.now()*0.004 + i) * 0.16;

      if(player.pos.distanceTo(p.mesh.position) < 1.5){
        if(p.kind === "ammo"){
          const gain = 12 + Math.floor(Math.random()*10);
          player.ammo.bullet += gain;
          showFloating(`AMMO +${gain}`);
        } else if(p.kind === "rocket"){
          const gain = 1 + (Math.random() < 0.35 ? 1 : 0);
          player.ammo.rocket += gain;
          showFloating(`ROCKET +${gain}`);
        } else if(p.kind === "grenade"){
          const gain = 1 + (Math.random() < 0.35 ? 1 : 0);
          player.ammo.grenade += gain;
          showFloating(`GRENADE +${gain}`);
        } else if(p.kind === "heal"){
          player.hp = Math.min(player.maxHp, player.hp + 24);
          showFloating("HEAL +24");
        } else if(p.kind === "shield"){
          player.hp = Math.min(player.maxHp, player.hp + 10);
          player.damageCooldown = 1.0;
          player.abilities.plasma = Math.min(5, player.abilities.plasma + 1);
          showFloating("SHIELD + PLASMA");
        }

        sfxPickup();
        scene.remove(p.mesh);
        state.pickups.splice(i,1);
        setStat();
        continue;
      }

      if(p.life <= 0){
        scene.remove(p.mesh);
        state.pickups.splice(i,1);
      }
    }
  }

  function updateTimers(dt){
    player.fireCooldown = Math.max(0, player.fireCooldown - dt);
    player.damageCooldown = Math.max(0, player.damageCooldown - dt);
    if(state.comboTimer > 0){
      state.comboTimer = Math.max(0, state.comboTimer - dt);
      if(state.comboTimer === 0) state.combo = 1;
    }

    if(totalAmmo() === 0 && player.alive){
      state.emergencyAmmoTimer += dt;
      if(state.emergencyAmmoTimer >= 1.15){
        player.ammo.bullet = Math.min(24, player.ammo.bullet + 6);
        state.emergencyAmmoTimer = 0;
        createBurst(player.pos.clone().add(new THREE.Vector3(0,1.2,0)), 0x86b6ff, 10, 3.2, { minLife:.2, maxLife:.55, gravity:2.1, shrink:0.96 });
        createFlash(player.pos.clone().add(new THREE.Vector3(0,1.4,0)), 0x86b6ff, 2.0, 7, 0.18);
        flashHint("Noodvoorraad actief: +6 bullets");
        sfxPickup();
      }
    } else {
      state.emergencyAmmoTimer = 0;
      if(state.ammoHintTimer && performance.now() > state.ammoHintTimer){
        state.ammoHintTimer = 0;
        restoreDefaultHint();
      }
    }

    ensureUsableWeapon();
    setStat();
  }

  function animate(now){
    requestAnimationFrame(animate);
    const dt = Math.min(0.033, (now - state.lastTime) / 1000 || 0.016);
    state.lastTime = now;

    stars.rotation.y += dt * 0.01;
    skyline.position.x = Math.sin(now * 0.00006) * 1.6;
    arenaDeco.crystalClusters.children.forEach((crystal, i) => {
      crystal.position.y = crystal.userData.baseY + Math.sin(now * 0.0011 + crystal.userData.floatOffset + i) * 0.08;
      crystal.rotation.y += dt * 0.35;
    });
    arenaDeco.fogWisps.children.forEach((fog, i) => {
      fog.position.x = fog.userData.base.x + Math.sin(now * 0.00025 * (i+1) + fog.userData.phase) * 3.2;
      fog.position.z = fog.userData.base.z + Math.cos(now * 0.0002 * (i+1) + fog.userData.phase) * 2.4;
      fog.position.y = fog.userData.base.y + Math.sin(now * 0.0008 + fog.userData.phase) * 0.22;
    });
    arenaDeco.searchlights.children.forEach((item, i) => {
      if(item.userData.spot){
        item.rotation.y = Math.sin(now * 0.00045 + item.userData.phase) * 1.2;
        item.rotation.x = -0.26 + Math.sin(now * 0.00033 + item.userData.phase) * 0.12;
        const spot = item.userData.spot;
        const dir = new THREE.Vector3(0, -1, -1).applyEuler(item.rotation).normalize();
        spot.position.copy(item.position);
        spot.target.position.copy(item.position).add(dir.multiplyScalar(36));
        spot.intensity = 1.15 + Math.sin(now * 0.0011 + i) * 0.18;
      }
    });
    arenaDeco.skyBands.children.forEach((band, i) => {
      band.position.y = band.userData.baseY + Math.sin(now * 0.00035 + band.userData.phase) * 1.6;
      band.position.x = Math.sin(now * 0.00022 + band.userData.phase) * 12;
      band.material.opacity = 0.05 + Math.sin(now * 0.0008 + band.userData.phase) * 0.03;
      band.rotation.z = Math.sin(now * 0.00012 + i) * 0.07;
    });
    arenaDeco.floatingShards.children.forEach((shard, i) => {
      shard.position.x = shard.userData.base.x + Math.sin(now * 0.0006 + shard.userData.phase + i) * 1.1;
      shard.position.y = shard.userData.base.y + Math.sin(now * 0.0012 + shard.userData.phase) * 0.48;
      shard.position.z = shard.userData.base.z + Math.cos(now * 0.00055 + shard.userData.phase) * 1.1;
      shard.rotation.x += dt * shard.userData.spin;
      shard.rotation.y += dt * shard.userData.spin * 0.8;
    });
    arenaDeco.monoliths.children.forEach((monolith, i) => {
      monolith.material.emissiveIntensity = 0.08 + Math.sin(now * 0.0011 + i) * 0.05;
    });
    emberField.children.forEach((ember, i) => {
      ember.position.y = ember.userData.baseY + Math.sin(now * 0.001 * ember.userData.speed + i) * 0.18;
      ember.position.x += Math.sin(now * 0.0002 + i) * 0.0008;
    });
    neonA.position.x = Math.sin(now * 0.00045) * 12;
    neonA.position.z = Math.cos(now * 0.00042) * 10;
    neonB.position.x = Math.cos(now * 0.0005) * -12;
    neonB.position.z = Math.sin(now * 0.00047) * 10;
    moonGlow.intensity = 0.7 + Math.sin(now * 0.00035) * 0.1;

    if(state.running){
      updateMusic();
      updateTimers(dt);
      updateMovement(dt);
      updateBullets(dt);
      updateEnemies(dt);
      updateParticles(dt);
      updateEffects(dt);
      updateRagdolls(dt);
      updatePickups(dt);
      tryAdvanceWave();

      if(state.fireHeld && !isTouch && player.weapon === "bullet"){
        shootWithDirection();
      }
    } else {
      updateViewWeapon(dt);
      updateEffects(dt);
      updateParticles(dt);
      updateRagdolls(dt);
    }

    drawMinimap();
    renderer.render(scene, camera);
  }

  function startGame(){
    ensureAudio();
    if(audioCtx && state.songClock < audioCtx.currentTime){
      state.songClock = audioCtx.currentTime + 0.05;
      state.songStep = -1;
    }
    if(collidesAt(player.pos.x, player.pos.z, player.radius)){
      resetPlayerPosition();
    }
    state.running = true;
    player.alive = true;
    ui.center.classList.add("hidden");
    if(!state.enemies.length && !state.boss) spawnWave();
    if(!isTouch) renderer.domElement.requestPointerLock?.();
  }

  ui.startBtn.addEventListener("click", startGame);
  ui.restartBtn.addEventListener("click", restartGame);

  document.addEventListener("pointerlockchange", () => {
    state.pointerLocked = document.pointerLockElement === renderer.domElement;
  });

  document.addEventListener("mousemove", e => {
    if(state.pointerLocked && state.running){
      lookYaw -= e.movementX * 0.0022;
      lookPitch -= e.movementY * 0.0017;
      lookPitch = clamp(lookPitch, -1.05, 1.05);
      applyCameraLook();
    }
  });

  window.addEventListener("keydown", e => {
    input.keyboard[e.code] = true;

    if(["ArrowUp","ArrowDown","ArrowLeft","ArrowRight","Space","Enter"].includes(e.code)){
      e.preventDefault();
    }

    if(e.code === "Digit1") setWeapon("bullet");
    if(e.code === "Digit2") setWeapon("rocket");
    if(e.code === "Digit3") setWeapon("grenade");
    if(e.code === "Digit4") firePlasmaBurst();
    if(e.code === "Digit5") deployShockMine();
    if(e.code === "Digit6") deployOrbital();

    if(e.code === "Space" || e.code === "Enter"){
      if(player.weapon === "bullet"){
        state.fireHeld = true;
      }
      shootWithDirection();
    }

    if(e.code === "KeyR" && !player.alive){
      restartGame();
    }
  }, { passive:false });

  window.addEventListener("keyup", e => {
    input.keyboard[e.code] = false;
    if(e.code === "Space" || e.code === "Enter"){
      state.fireHeld = false;
    }
  });

  renderer.domElement.addEventListener("mousedown", e => {
    if(e.button !== 0 || isTouch) return;
    ensureAudio();
    if(!state.running) return;
    if(player.weapon === "bullet") state.fireHeld = true;
    shootWithDirection();
  });

  window.addEventListener("mouseup", e => {
    if(e.button === 0) state.fireHeld = false;
  });

  renderer.domElement.addEventListener("click", () => {
    if(!isTouch && state.running && !state.pointerLocked){
      renderer.domElement.requestPointerLock?.();
    }
  });

  renderer.domElement.addEventListener("pointerdown", e => {
    if(!isTouch) return;
    if(!state.running){
      startGame();
    }

    const joyRect = document.getElementById("joy").getBoundingClientRect();
    const insideJoy = e.clientX >= joyRect.left && e.clientX <= joyRect.right && e.clientY >= joyRect.top && e.clientY <= joyRect.bottom;
    const uiRect = document.getElementById("ui").getBoundingClientRect();
    const insideUi = e.clientX >= uiRect.left && e.clientX <= uiRect.right && e.clientY >= uiRect.top && e.clientY <= uiRect.bottom;
    const abilityRect = document.getElementById("abilityDock")?.getBoundingClientRect();
    const insideAbility = abilityRect && e.clientX >= abilityRect.left && e.clientX <= abilityRect.right && e.clientY >= abilityRect.top && e.clientY <= abilityRect.bottom;

    if(!insideJoy && !insideUi && !insideAbility){
      touchShootAt(e.clientX, e.clientY);
    }
  });

  const joy = document.getElementById("joy");
  const joyKnob = document.getElementById("joyKnob");
  const lookJoy = document.getElementById("lookJoy");
  const lookJoyKnob = document.getElementById("lookJoyKnob");
  const touchState = { moveId:null, lookId:null };

  function setJoy(knob, dx, dy){
    knob.style.left = (37 + dx * 34) + "px";
    knob.style.top = (37 + dy * 34) + "px";
  }

  function handleMoveJoystick(dx, dy){
    setJoy(joyKnob, dx, dy);
    input.keyboard["KeyW"] = dy < -0.18;
    input.keyboard["KeyS"] = dy > 0.18;
    input.keyboard["KeyA"] = dx < -0.18;
    input.keyboard["KeyD"] = dx > 0.18;
  }

  function handleLookJoystick(dx, dy){
    setJoy(lookJoyKnob, dx, dy);
    input.lookX = Math.abs(dx) > 0.08 ? dx : 0;
    input.lookY = Math.abs(dy) > 0.08 ? dy : 0;
  }

  function readStick(e, el){
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width/2;
    const cy = r.top + r.height/2;
    let dx = (e.clientX - cx) / (r.width/2);
    let dy = (e.clientY - cy) / (r.height/2);
    const len = Math.hypot(dx,dy) || 1;
    if(len > 1){ dx /= len; dy /= len; }
    return {dx,dy};
  }

  joy.addEventListener("pointerdown", e => {
    touchState.moveId = e.pointerId;
    joy.setPointerCapture(e.pointerId);
    ensureAudio();
    if(!state.running) startGame();
  });

  joy.addEventListener("pointermove", e => {
    if(touchState.moveId !== e.pointerId) return;
    const {dx,dy} = readStick(e, joy);
    handleMoveJoystick(dx, dy);
  });

  lookJoy.addEventListener("pointerdown", e => {
    touchState.lookId = e.pointerId;
    lookJoy.setPointerCapture(e.pointerId);
    ensureAudio();
    if(!state.running) startGame();
  });

  lookJoy.addEventListener("pointermove", e => {
    if(touchState.lookId !== e.pointerId) return;
    const {dx,dy} = readStick(e, lookJoy);
    handleLookJoystick(dx, dy);
  });

  function releaseMoveJoy(){
    touchState.moveId = null;
    handleMoveJoystick(0,0);
  }

  function releaseLookJoy(){
    touchState.lookId = null;
    handleLookJoystick(0,0);
  }

  joy.addEventListener("pointerup", releaseMoveJoy);
  joy.addEventListener("pointercancel", releaseMoveJoy);
  lookJoy.addEventListener("pointerup", releaseLookJoy);
  lookJoy.addEventListener("pointercancel", releaseLookJoy);


  [
    [ui.chipBullet, () => setWeapon("bullet")],
    [ui.chipRocket, () => setWeapon("rocket")],
    [ui.chipGrenade, () => setWeapon("grenade")],
    [ui.chipPlasma, firePlasmaBurst],
    [ui.chipMine, deployShockMine],
    [ui.chipOrbital, deployOrbital]
  ].forEach(([btn, fn]) => {
    btn?.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio();
      if(!state.running) startGame();
      fn();
    });
  });

  [
    [ui.abilityPlasma, firePlasmaBurst],
    [ui.abilityMine, deployShockMine],
    [ui.abilityOrbital, deployOrbital]
  ].forEach(([btn, fn]) => {
    btn?.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio();
      if(!state.running) startGame();
      fn();
    });
  });

  window.addEventListener("resize", () => {
    camera.aspect = innerWidth / innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(innerWidth, innerHeight);
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  });

  animate(performance.now());
})();

</script>
</body>
</html>
"""


PRIVACY_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Privacyverklaring – downloadlink.nl</title>{{ head_icon|safe }}
<style>
{{ base_css }}
h1{color:var(--brand);margin:.2rem 0 1rem}
h2{margin:1.2rem 0 .4rem}
.section{margin-bottom:1.1rem}
.small{color:#475569}
.card p{margin:.45rem 0}
ul{margin:.4rem 0 .6rem 1.2rem}
</style></head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card">
    <h1>Privacyverklaring – downloadlink.nl</h1>
    <p class="small">Versie: 1.0 • Laatst bijgewerkt: 25-09-2025</p>

    <div class="section">
      <h2>1. Wie zijn wij?</h2>
      <p>downloadlink.nl is verantwoordelijk voor de verwerking van persoonsgegevens zoals beschreven in deze verklaring. 
         Voor vragen kun je ons bereiken via <a href="mailto:{{ mail_to }}">{{ mail_to }}</a>.</p>
    </div>

    <div class="section">
      <h2>2. Welke gegevens verwerken wij?</h2>
      <ul>
        <li><strong>Contactgegevens</strong>: naam, e-mailadres, bedrijfsnaam (via het aanvraagformulier).</li>
        <li><strong>Account- en betaalgegevens</strong>: klantnummer, gekozen plan, PayPal-transactiegegevens (geen creditcardnummers).</li>
        <li><strong>Gebruiksgegevens</strong>: logbestanden, IP-adressen, browserinformatie, bestandsuploads.</li>
        <li><strong>Communicatie</strong>: e-mails of supportvragen.</li>
      </ul>
    </div>

    <div class="section">
      <h2>3. Waarvoor gebruiken wij deze gegevens?</h2>
      <ul>
        <li>Uitvoering van de overeenkomst (hosting & bestandsuitwisseling, facturatie, support).</li>
        <li>Beveiliging en beschikbaarheid van de dienst (monitoring, misbruikdetectie).</li>
        <li>Wettelijke verplichtingen (administratie, belastingregels).</li>
        <li>Contact en klantenservice.</li>
      </ul>
    </div>

    <div class="section">
      <h2>4. Op welke grondslagen?</h2>
      <ul>
        <li>Uitvoering van een overeenkomst (dienstverlening en betalingen).</li>
        <li>Wettelijke verplichting (bewaarplicht administratie).</li>
        <li>Gerechtvaardigd belang (veiligheid, misbruikpreventie, zakelijke communicatie).</li>
      </ul>
    </div>

    <div class="section">
      <h2>5. Hoe lang bewaren wij gegevens?</h2>
      <p>Wij bewaren persoonsgegevens niet langer dan noodzakelijk. Administratieve en facturatiegegevens: <strong>7 jaar</strong> (wettelijke bewaarplicht). 
         Account- en gebruiksgegevens: maximaal <strong>12 maanden</strong> na beëindiging van de dienst, tenzij langer vereist door wetgeving.</p>
    </div>

    <div class="section">
      <h2>6. Met wie delen wij gegevens?</h2>
      <p>Wij delen gegevens uitsluitend indien noodzakelijk met:</p>
      <ul>
        <li>Onze hostingprovider (S3-compatibele opslag, serverbeheer).</li>
        <li>Onze betaalprovider (PayPal) voor verwerking van betalingen.</li>
        <li>Onze mailprovider voor transactieberichten en support.</li>
      </ul>
      <p>Met deze partijen zijn verwerkersovereenkomsten gesloten. 
         Buiten de EU zorgen wij voor passende waarborgen (zoals EU-modelclausules).</p>
    </div>

    <div class="section">
      <h2>7. Jouw rechten</h2>
      <p>Je hebt het recht om:</p>
      <ul>
        <li>Inzage te vragen in jouw persoonsgegevens.</li>
        <li>Correctie of verwijdering te verzoeken.</li>
        <li>Bezwaar te maken tegen verwerking of beperking te vragen.</li>
        <li>Gegevensoverdracht te vragen (dataportabiliteit).</li>
        <li>Een klacht in te dienen bij de Autoriteit Persoonsgegevens.</li>
      </ul>
      <p>Verzoeken kun je sturen naar <a href="mailto:{{ mail_to }}">{{ mail_to }}</a>. 
         Wij reageren binnen 30 dagen.</p>
    </div>

    <div class="section">
      <h2>8. Beveiliging</h2>
      <p>Wij nemen passende technische en organisatorische maatregelen om persoonsgegevens te beveiligen tegen misbruik, verlies, onbevoegde toegang, 
         ongewenste openbaarmaking en ongeoorloofde wijziging.</p>
    </div>

    <div class="section small">
      <p>Vragen? Neem gerust contact op via <a href="mailto:{{ mail_to }}">{{ mail_to }}</a>.</p>
    </div>
  </div>
  <p class="footer">downloadlink.nl • Privacyverklaring</p>
</div>
</body></html>
"""





def is_valid_token(token: str) -> bool:
    return bool(token and TOKEN_RE.fullmatch(token))

def clamp_expiry_days(value) -> float:
    try:
        days = float(value)
    except (TypeError, ValueError):
        return 24.0
    return max(MIN_EXPIRY_DAYS, min(MAX_EXPIRY_DAYS, days))

def normalize_rel_path(value: str, fallback: str) -> str:
    raw = (value or fallback or "").replace(chr(92), "/").strip().lstrip("/")
    parts = [part for part in raw.split("/") if part not in {"", ".", ".."}]
    return "/".join(parts) or secure_filename(fallback or "bestand")

@app.before_request
def attach_request_context():
    g.request_id = uuid.uuid4().hex[:12]

@app.after_request
def apply_default_headers(resp):
    resp.headers.setdefault("X-Request-ID", getattr(g, "request_id", "-"))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.path.startswith(("/login", "/logout")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp

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
    if not to_addr or not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        log.warning("E-mail niet verstuurd: SMTP niet (volledig) geconfigureerd")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)

def paypal_access_token():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID/SECRET ontbreekt")
    req = urllib.request.Request(PAYPAL_API_BASE + "/v1/oauth2/token", method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    creds = f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()
    req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode())
    data = "grant_type=client_credentials".encode()
    with urllib.request.urlopen(req, data=data, timeout=20) as resp:
        j = json.loads(resp.read().decode())
        return j["access_token"]

# --------- Basishost voor subdomein-preview ----------
def get_base_host():
    # Altijd downloadlink.nl gebruiken voor voorbeeldlink (ongeacht host)
    return "downloadlink.nl"


# ------------- Favicon -------------
FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="12" fill="#1E3A8A"/>
  <text x="50%" y="55%" text-anchor="middle" dominant-baseline="middle"
        font-family="Segoe UI, Roboto, sans-serif" font-size="28" font-weight="700"
        fill="white">OH</text>
</svg>"""

# -------------- Routes (core) --------------
# Opgeschoond: dubbele routeblokken verwijderd en configuratie iets robuuster gemaakt.

@app.route("/debug/dbcols")
def debug_dbcols():
    c = db()
    out = {}
    for table in ["packages", "items", "subscriptions"]:
        cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
        out[table] = cols
    rows = c.execute("SELECT DISTINCT tenant_id FROM packages").fetchall()
    out["tenants_in_packages"] = [r[0] for r in rows]
    c.close()
    return jsonify(out)

@app.route("/")
def index():
    if not logged_in(): return redirect(url_for("login"))
    return render_template_string(INDEX_HTML, user=session.get("user"), base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        email = (request.form.get("email") or "").lower().strip()
        # accept either the hidden 'password' or the UI field 'pw_ui'
        pw    = (request.form.get("password") or request.form.get("pw_ui") or "").strip()

        if email == AUTH_EMAIL and pw == AUTH_PASSWORD:
            session.clear()
            session.permanent = True
            session["authed"] = True
            session["user"] = AUTH_EMAIL
            return redirect(url_for("index"))

        return render_template_string(
            LOGIN_HTML,
            error="Onjuiste inloggegevens.",
            base_css=BASE_CSS, bg=BG_DIV,
            auth_email=AUTH_EMAIL,
            head_icon=HTML_HEAD_ICON
        )

    return render_template_string(
        LOGIN_HTML,
        error=None,
        base_css=BASE_CSS, bg=BG_DIV,
        auth_email=AUTH_EMAIL,
        head_icon=HTML_HEAD_ICON
    )

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# -------------- Upload API --------------
@app.route("/package-init", methods=["POST"])
def package_init():
    if not logged_in(): abort(401)
    data = request.get_json(force=True, silent=True) or {}
    days = clamp_expiry_days(data.get("expiry_days") or 24)
    pw   = (data.get("password") or "")[:200]
    title_raw = (data.get("title") or "").strip()
    title = title_raw[:MAX_TITLE_LENGTH] if title_raw else None
    token = uuid.uuid4().hex[:10]
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    pw_hash = generate_password_hash(pw) if pw else None
    t = current_tenant()["slug"]
    c = db()
    c.execute("""INSERT INTO packages(token,expires_at,password_hash,created_at,title,tenant_id)
                 VALUES(?,?,?,?,?,?)""",
              (token, expires_at, pw_hash, datetime.now(timezone.utc).isoformat(), title, t))
    c.commit(); c.close()
    return jsonify(ok=True, token=token)
    
@app.route("/put-init", methods=["POST"])
def put_init():
    if not logged_in(): abort(401)
    d = request.get_json(force=True, silent=True) or {}
    token = (d.get("token") or "").strip(); filename = secure_filename(d.get("filename") or "")
    content_type = (d.get("contentType") or "application/octet-stream").strip() or "application/octet-stream"
    if not is_valid_token(token) or not filename:
        return jsonify(ok=False, error="Onvolledige init (PUT)"), 400
    t = current_tenant()["slug"]
    key = f"uploads/{t}/{token}/{uuid.uuid4().hex[:8]}__{filename}"
    try:
        url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": S3_BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=3600, HttpMethod="PUT"
        )
        return jsonify(ok=True, key=key, url=url)
    except Exception:
        log.exception("put_init failed")
        return jsonify(ok=False, error="server_error"), 500

@app.route("/put-complete", methods=["POST"])
def put_complete():
    if not logged_in(): abort(401)
    d = request.get_json(force=True, silent=True) or {}
    token = (d.get("token") or "").strip(); key = (d.get("key") or "").strip(); name = (d.get("name") or "").strip()
    path  = normalize_rel_path(d.get("path") or name, name)
    if not (is_valid_token(token) and key and name):
        return jsonify(ok=False, error="Onvolledig afronden (PUT)"), 400
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
        size = int(head.get("ContentLength", 0))
        t = current_tenant()["slug"]
        c = db()
        c.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes,tenant_id)
                     VALUES(?,?,?,?,?,?)""",
                  (token, key, name, path, size, t))
        c.commit(); c.close()
        return jsonify(ok=True)
    except (ClientError, BotoCoreError):
        log.exception("put_complete failed")
        return jsonify(ok=False, error="server_error"), 500

@app.route("/mpu-init", methods=["POST"])
def mpu_init():
    if not logged_in(): abort(401)
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()
    filename = secure_filename(data.get("filename") or "")
    content_type = (data.get("contentType") or "application/octet-stream").strip() or "application/octet-stream"
    if not is_valid_token(token) or not filename:
        return jsonify(ok=False, error="Onvolledige init (MPU)"), 400
    t = current_tenant()["slug"]
    key = f"uploads/{t}/{token}/{uuid.uuid4().hex[:8]}__{filename}"
    try:
        init = s3.create_multipart_upload(Bucket=S3_BUCKET, Key=key, ContentType=content_type)
        return jsonify(ok=True, key=key, uploadId=init["UploadId"])
    except Exception:
        log.exception("mpu_init failed")
        return jsonify(ok=False, error="server_error"), 500

@app.route("/mpu-sign", methods=["POST"])
def mpu_sign():
    if not logged_in(): abort(401)
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key"); upload_id = data.get("uploadId")
    part_no = int(data.get("partNumber") or 0)
    if not key or not upload_id or part_no<=0:
        return jsonify(ok=False, error="Onvolledig sign"), 400
    try:
        url = s3.generate_presigned_url(
            "upload_part",
            Params={"Bucket": S3_BUCKET, "Key": key, "UploadId": upload_id, "PartNumber": part_no},
            ExpiresIn=3600, HttpMethod="PUT"
        )
        return jsonify(ok=True, url=url)
    except Exception:
        log.exception("mpu_sign failed")
        return jsonify(ok=False, error="server_error"), 500

@app.route("/mpu-complete", methods=["POST"])
def mpu_complete():
    if not logged_in(): abort(401)
    data      = request.get_json(force=True, silent=True) or {}
    token     = (data.get("token") or "").strip(); key = (data.get("key") or "").strip()
    name      = (data.get("name") or "").strip();  path = normalize_rel_path(data.get("path") or name, name)
    parts_in  = data.get("parts") or []; upload_id = data.get("uploadId")
    client_size = int(data.get("clientSize") or 0)
    if not (is_valid_token(token) and key and name and parts_in and upload_id):
        return jsonify(ok=False, error="Onvolledig afronden (ontbrekende velden)"), 400
    try:
        s3.complete_multipart_upload(
            Bucket=S3_BUCKET, Key=key,
            MultipartUpload={"Parts": sorted(parts_in, key=lambda p: p["PartNumber"])},
            UploadId=upload_id
        )
        size = 0
        try:
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            size = int(head.get("ContentLength", 0))
        except Exception:
            if client_size>0: size = client_size
            else: raise
        t = current_tenant()["slug"]
        c = db()
        c.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes,tenant_id)
                     VALUES(?,?,?,?,?,?)""",
                  (token, key, name, path, size, t))
        c.commit(); c.close()
        return jsonify(ok=True)
    except (ClientError, BotoCoreError) as e:
        log.exception("mpu_complete failed")
        return jsonify(ok=False, error=f"mpu_complete_failed:{getattr(e,'response',{})}"), 500
    except Exception:
        log.exception("mpu_complete failed (generic)")
        return jsonify(ok=False, error="server_error"), 500
        
@app.post("/internal/cleanup")
def internal_cleanup():
    """
    Interne route voor cron. Verwijdert verlopen pakketten + S3-objecten.
    Auth via header: X-Task-Token  (zet TASK_TOKEN als secret in Render).
    Opties:
      - ?dry=1  -> dry-run (niets echt verwijderen)
      - ?tenant=slug  -> alleen die tenant (bijv. 'oldehanter')
      - ?verbose=1 -> extra logging in response
    """
    task_token = os.environ.get("TASK_TOKEN")
    if not task_token or request.headers.get("X-Task-Token") != task_token:
        return ("Forbidden", 403)

    dry = request.args.get("dry") in {"1", "true", "yes"}
    only_tenant = request.args.get("tenant") or None
    verbose = request.args.get("verbose") in {"1", "true", "yes"}

    # Prefer de DB die de app zelf gebruikt; fallback naar resolver
    db_path = DB_PATH if DB_PATH.exists() else (resolve_data_dir(verbose=verbose) / "files_multi.db")

    try:
        deleted = cleanup_expired(
            db_path=db_path,
            dry_run=dry,
            only_tenant=only_tenant,
            verbose=verbose,
        )
        return jsonify(ok=True, deleted=deleted, db=str(db_path), dry=dry, tenant=only_tenant)
    except Exception as e:
        logging.exception("internal_cleanup failed")
        return jsonify(ok=False, error=str(e), db=str(db_path)), 500

# -------------- Download Pages --------------
@app.route("/p/<token>", methods=["GET","POST"])
def package_page(token):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)
    c = db()
    t = current_tenant()["slug"]
    pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
    if not pkg:
        return render_template_string(
            EXPIRED_HTML,
            base_css=BASE_CSS,
            bg=BG_DIV,
            head_icon=HTML_HEAD_ICON
        ), 404

    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc):
        rows = c.execute("SELECT s3_key FROM items WHERE token=? AND tenant_id=?", (token, t)).fetchall()
        for r in rows:
            try: s3.delete_object(Bucket=S3_BUCKET, Key=r["s3_key"])
            except Exception: pass
        c.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, t))
        c.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, t))
        c.commit(); c.close(); abort(410)

    if pkg["password_hash"]:
        if request.method == "GET" and not session.get(f"allow_{token}", False):
            c.close()
            return render_template_string(PASS_PROMPT_HTML, base_css=BASE_CSS, bg=BG_DIV, error=None, head_icon=HTML_HEAD_ICON)
        if request.method == "POST":
            if not check_password_hash(pkg["password_hash"], request.form.get("password","")):
                c.close()
                return render_template_string(PASS_PROMPT_HTML, base_css=BASE_CSS, bg=BG_DIV, error="Onjuist wachtwoord. Probeer opnieuw.", head_icon=HTML_HEAD_ICON)
            session[f"allow_{token}"] = True

    items = c.execute("""SELECT id,name,path,size_bytes FROM items
                         WHERE token=? AND tenant_id=?
                         ORDER BY path""", (token, t)).fetchall()
    c.close()

    total_bytes = sum(int(r["size_bytes"]) for r in items)
    total_h = human(total_bytes)
    dt = datetime.fromisoformat(pkg["expires_at"]).replace(second=0, microsecond=0)
    expires_h = dt.strftime("%d-%m-%Y %H:%M")

    its = [{"id":r["id"], "name":r["name"], "path":r["path"], "size_h":human(int(r["size_bytes"]))} for r in items]

    return render_template_string(
        PACKAGE_HTML,
        token=token, title=pkg["title"],
        items=its, total_human=total_h,
        expires_human=expires_h, base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON
    )

@app.route("/file/<token>/<int:item_id>")
def stream_file(token, item_id):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)
    c = db()
    t = current_tenant()["slug"]
    pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
    if not pkg: c.close(); abort(404)
    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc): c.close(); abort(410)
    if pkg["password_hash"] and not session.get(f"allow_{token}", False): c.close(); abort(403)
    it = c.execute("SELECT * FROM items WHERE id=? AND token=? AND tenant_id=?", (item_id, token, t)).fetchone()
    c.close()
    if not it: abort(404)

    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=it["s3_key"])
        length = int(head.get("ContentLength", 0))
        obj = s3.get_object(Bucket=S3_BUCKET, Key=it["s3_key"])

        def gen():
            for chunk in obj["Body"].iter_chunks(1024*512):
                if chunk: yield chunk

        resp = Response(stream_with_context(gen()), mimetype="application/octet-stream")
        resp.headers["Content-Disposition"] = f'attachment; filename="{it["name"]}"'
        if length: resp.headers["Content-Length"] = str(length)
        resp.headers["X-Filename"] = it["name"]
        return resp
    except Exception:
        log.exception("stream_file failed")
        abort(500)

@app.route("/zip/<token>")
def stream_zip(token):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)
    c = db()
    t = current_tenant()["slug"]
    pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
    if not pkg: c.close(); abort(404)
    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc): c.close(); abort(410)
    if pkg["password_hash"] and not session.get(f"allow_{token}", False): c.close(); abort(403)
    rows = c.execute("""SELECT name,path,s3_key FROM items
                        WHERE token=? AND tenant_id=?
                        ORDER BY path""", (token, t)).fetchall()
    c.close()
    if not rows: abort(404)

    # Precheck ontbrekende objecten
    missing=[]
    try:
        for r in rows:
            try: s3.head_object(Bucket=S3_BUCKET, Key=r["s3_key"])
            except ClientError as ce:
                code=ce.response.get("Error",{}).get("Code","")
                if code in {"NoSuchKey","NotFound","404"}: missing.append(r["path"] or r["name"])
                else: raise
    except Exception:
        log.exception("zip precheck failed")
        resp=Response("ZIP precheck mislukt. Zie serverlogs.", status=500, mimetype="text/plain")
        resp.headers["X-Error"]="zip_precheck_failed"; return resp
    if missing:
        text="De volgende items ontbreken in S3 en kunnen niet gezipt worden:\n- " + "\n- ".join(missing)
        resp=Response(text, mimetype="text/plain", status=422)
        resp.headers["X-Error"]="NoSuchKey: " + ", ".join(missing); return resp

    try:
        z = ZipStream()

        class _GenReader:
            def __init__(self, gen): self._it = gen; self._buf=b""; self._done=False
            def read(self, n=-1):
                if self._done and not self._buf: return b""
                if n is None or n<0:
                    chunks=[self._buf]; self._buf=b""
                    for ch in self._it: chunks.append(ch)
                    self._done=True; return b"".join(chunks)
                while len(self._buf)<n and not self._done:
                    try: self._buf += next(self._it)
                    except StopIteration: self._done=True; break
                out,self._buf=self._buf[:n],self._buf[n:]; return out

        def add_compat(arcname, gen_factory):
            if hasattr(z,"add_iter"):
                try: z.add_iter(arcname, gen_factory()); return
                except Exception: pass
            try: z.add(arcname=arcname, iterable=gen_factory()); return
            except Exception: pass
            try: z.add(arcname=arcname, stream=gen_factory()); return
            except Exception: pass
            try: z.add(arcname=arcname, fileobj=_GenReader(gen_factory())); return
            except Exception: pass
            try: z.add(arcname, gen_factory()); return
            except Exception: pass
            try: z.add(gen_factory(), arcname); return
            except Exception: pass
            raise RuntimeError("Geen compatibele zipstream-ng add() signatuur gevonden")

        for r in rows:
            arcname = r["path"] or r["name"]
            def reader(key=r["s3_key"]):
                obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
                for chunk in obj["Body"].iter_chunks(1024*512):
                    if chunk: yield chunk
            add_compat(arcname, lambda: reader())

        def generate():
            for chunk in z: yield chunk

        filename = (pkg["title"] or f"onderwerp-{token}").strip().replace('"','')
        if not filename.lower().endswith(".zip"): filename += ".zip"

        resp = Response(stream_with_context(generate()), mimetype="application/zip")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp.headers["X-Filename"] = filename
        return resp
    except Exception as e:
        log.exception("stream_zip failed")
        msg = f"ZIP generatie mislukte. Err: {e}"
        resp = Response(msg, status=500, mimetype="text/plain")
        resp.headers["X-Error"] = "zipstream_failed"
        return resp
        
@app.route("/terms")
def terms_page():
    return render_template_string(
        TERMS_HTML,
        base_css=BASE_CSS,
        bg=BG_DIV,
        head_icon=HTML_HEAD_ICON,
        mail_to=MAIL_TO
    )
# -------------- Contact / Mail --------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE  = re.compile(r"^[0-9+()\\s-]{8,20}$")
ALLOWED_TB = {0.5, 1.0, 2.0, 5.0}
PRICE_LABEL = {0.5:"€12/maand", 1.0:"€15/maand", 2.0:"€20/maand", 5.0:"€30/maand"}

def _send_contact_email(form):
    storage_val = form.get("storage_tb")
    if storage_val in PRICE_LABEL:
        price_label = PRICE_LABEL[storage_val]  # type: ignore[index]
        storage_line = f"- Gewenste opslag: {storage_val} TB (indicatie {price_label})\n"
    else:
        storage_line = "- Gewenste opslag: meer opslag (op aanvraag)\n"

    base_host = form.get("base_host") or get_base_host()
    company_slug = form.get("company_slug") or ""
    example_link  = f"{company_slug}.{base_host}" if company_slug else base_host

    body = (
        "Er is een nieuwe aanvraag binnengekomen:\n\n"
        f"- Gewenste inlog-e-mail: {form['login_email']}\n"
        f"{storage_line}"
        f"- Bedrijfsnaam: {form['company']}\n"
        f"- Telefoonnummer: {form['phone']}\n"
        f"- Wachtwoord: {form.get('desired_password','(niet ingevuld)')}\n"
        f"- Subdomein voorbeeld: {example_link}\n"
        f"- Opmerking: {form.get('notes') or '-'}\n\n"
        "Livegang: doorgaans 1–2 dagen (langer bij maatwerk).\n"
        "Facturatie: PayPal abonnement mogelijk via site; of incasso-link per e-mail na livegang.\n"
    )

    send_email(MAIL_TO, "Nieuwe aanvraag transfer-oplossing", body)

@app.route("/contact", methods=["GET","POST"])
def contact():
    base_host = get_base_host()
    if request.method == "GET":
        return render_template_string(
            CONTACT_HTML, error=None,
            form={"login_email":"", "storage_tb":"", "company":"", "phone":"", "notes":""},
            base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON,
            base_host=base_host,
            paypal_client_id=PAYPAL_CLIENT_ID or "",
            paypal_plan_0_5=PAYPAL_PLAN_0_5 or "",
            paypal_plan_1=PAYPAL_PLAN_1 or "",
            paypal_plan_2=PAYPAL_PLAN_2 or "",
            paypal_plan_5=PAYPAL_PLAN_5 or ""
        )

    login_email   = (request.form.get("login_email") or "").strip()
    storage_tb_raw= (request.form.get("storage_tb") or "").strip()
    company       = (request.form.get("company") or "").strip()
    phone         = (request.form.get("phone") or "").strip()
    desired_pw    = (request.form.get("desired_password") or "").strip()
    notes         = (request.form.get("notes") or "").strip()

    errors = []
    if not EMAIL_RE.match(login_email): errors.append("Vul een geldig e-mailadres in.")

    is_more = (storage_tb_raw.lower() == "more")
    storage_tb = None
    if not storage_tb_raw:
        errors.append("Kies een geldige opslaggrootte.")
    elif not is_more:
        try:
            storage_tb = float(storage_tb_raw.replace(",", "."))
        except Exception:
            storage_tb = None
        if storage_tb not in ALLOWED_TB:
            errors.append("Kies een geldige opslaggrootte.")

    if len(company) < 2 or len(company) > 100: errors.append("Vul een geldige bedrijfsnaam in (min. 2 tekens).")
    if not PHONE_RE.match(phone): errors.append("Vul een geldig telefoonnummer in (8–20 tekens).")
    if len(desired_pw) < 6: errors.append("Kies een wachtwoord van minimaal 6 tekens.")

    form_back = {"login_email":login_email,"storage_tb":(storage_tb_raw or ""),
                 "company":company,"phone":phone,"notes":notes}

    if errors:
        return render_template_string(
            CONTACT_HTML, error=" ".join(errors),
            form=form_back, base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON,
            base_host=base_host,
            paypal_client_id=PAYPAL_CLIENT_ID or "",
            paypal_plan_0_5=PAYPAL_PLAN_0_5 or "",
            paypal_plan_1=PAYPAL_PLAN_1 or "",
            paypal_plan_2=PAYPAL_PLAN_2 or "",
            paypal_plan_5=PAYPAL_PLAN_5 or ""
        )

    # Slug voor voorbeeld subdomein
    def slugify_py(s: str) -> str:
        import unicodedata, re as _re
        s = unicodedata.normalize('NFKD', s)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.lower().replace("&"," en ")
        s = _re.sub(r"[^a-z0-9]+","-", s)
        s = _re.sub(r"^-+|-+$","", s)
        s = _re.sub(r"--+","-", s)
        return s[:50] if s else ""
    company_slug = slugify_py(company)

    # E-mail naar beheerder
    try:
        if SMTP_HOST and SMTP_USER and SMTP_PASS:
            _send_contact_email({
                "login_email": login_email,
                "storage_tb": (storage_tb if not is_more else "more"),
                "company": company,
                "phone": phone,
                "desired_password": desired_pw,
                "notes": notes,
                "company_slug": company_slug,
                "base_host": base_host
            })
            return render_template_string(
                CONTACT_DONE_HTML, base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON
            )
    except Exception:
        log.exception("contact mail failed")

    # Fallback: mailto
    if storage_tb in PRICE_LABEL:
        price_label = PRICE_LABEL[storage_tb]  # type: ignore[index]
        storage_line = f"- Gewenste opslag: {storage_tb} TB (indicatie {price_label})\\n"
    else:
        storage_line = "- Gewenste opslag: meer opslag (op aanvraag)\\n"

    example_link = f"{company_slug}.{base_host}" if company_slug else base_host
    body = (
        "Er is een nieuwe aanvraag binnengekomen:\\n\\n"
        f"- Gewenste inlog-e-mail: {login_email}\\n"
        f"{storage_line}"
        f"- Bedrijfsnaam: {company}\\n"
        f"- Telefoonnummer: {phone}\\n"
        f"- Wachtwoord: {desired_pw}\\n"
        f"- Subdomein voorbeeld: {example_link}\\n"
        f"- Opmerking: {notes or '-'}\\n\\n"
        "Livegang: doorgaans 1–2 dagen (langer bij maatwerk).\\n"
        "Facturatie: PayPal abonnement mogelijk via site; of incasso-link per e-mail na livegang.\\n"
    )
    from urllib.parse import quote
    mailto = f"mailto:{MAIL_TO}?subject={quote('Nieuwe aanvraag transfer-oplossing')}&body={quote(body)}"
    return render_template_string(CONTACT_MAIL_FALLBACK_HTML, mailto_link=mailto, base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON)

@app.route("/privacy")
def privacy_page():
    return render_template_string(
        PRIVACY_HTML,
        base_css=BASE_CSS,
        bg=BG_DIV,
        head_icon=HTML_HEAD_ICON,
        mail_to=MAIL_TO
    )    

# -------------- Abonnementbeheer (server) --------------
@app.route("/billing/store", methods=["POST"])
def paypal_store_subscription():
    data = request.get_json(force=True, silent=True) or {}
    sub_id = (data.get("subscription_id") or "").strip()
    plan_value = (data.get("plan_value") or "").strip()
    if not sub_id or plan_value not in {"0.5","1","2","5"}:
        return jsonify(ok=False, error="invalid_input"), 400
    t = current_tenant()["slug"]
    c = db()
    c.execute("""INSERT OR REPLACE INTO subscriptions(login_email, plan_value, subscription_id, status, created_at, tenant_id)
                 VALUES(?,?,?,?,?,?)""",
              (AUTH_EMAIL, plan_value, sub_id, "ACTIVE", datetime.now(timezone.utc).isoformat(), t))
    c.commit(); c.close()

    try:
        plan_label = {"0.5":"0,5 TB","1":"1 TB","2":"2 TB","5":"5 TB"}.get(plan_value, plan_value+" TB")
        body = (
            "Er is zojuist een PayPal-abonnement gestart (via onApprove):\n\n"
            f"- Subscription ID: {sub_id}\n"
            f"- Plan: {plan_label}\n"
            f"- Inlog-e-mail (klant in systeem): {AUTH_EMAIL}\n"
            f"- Datum/tijd (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        send_email(MAIL_TO, "Nieuwe PayPal-abonnement gestart", body)
    except Exception:
        log.exception("Kon bevestigingsmail niet versturen")
    return jsonify(ok=True)

# -------------- PayPal Webhook --------------
def paypal_verify_webhook_sig(headers, body_text: str) -> bool:
    """Verifieer webhook via /v1/notifications/verify-webhook-signature"""
    if not PAYPAL_WEBHOOK_ID:
        log.error("PAYPAL_WEBHOOK_ID ontbreekt; webhook niet te verifiëren.")
        return False
    try:
        transmission_id  = headers.get("Paypal-Transmission-Id") or headers.get("PayPal-Transmission-Id")
        timestamp        = headers.get("Paypal-Transmission-Time") or headers.get("PayPal-Transmission-Time")
        cert_url         = headers.get("Paypal-Cert-Url") or headers.get("PayPal-Cert-Url")
        auth_algo        = headers.get("Paypal-Auth-Algo") or headers.get("PayPal-Auth-Algo")
        transmission_sig = headers.get("Paypal-Transmission-Sig") or headers.get("PayPal-Transmission-Sig")
        if not all([transmission_id, timestamp, cert_url, auth_algo, transmission_sig]):
            log.warning("Webhook headers incompleet")
            return False
        token = paypal_access_token()
        payload = json.dumps({
            "auth_algo": auth_algo,
            "cert_url": cert_url,
            "transmission_id": transmission_id,
            "transmission_sig": transmission_sig,
            "transmission_time": timestamp,
            "webhook_id": PAYPAL_WEBHOOK_ID,
            "webhook_event": json.loads(body_text)
        }).encode()
        req = urllib.request.Request(PAYPAL_API_BASE + "/v1/notifications/verify-webhook-signature", method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, data=payload, timeout=20) as resp:
            v = json.loads(resp.read().decode())
            return (v.get("verification_status","").upper() == "SUCCESS")
    except Exception:
        log.exception("paypal_verify_webhook_sig failed")
        return False

@app.route("/webhook/paypal", methods=["POST"])
def paypal_webhook():
    body_text = request.get_data(as_text=True) or ""
    if not body_text:
        return jsonify(ok=False, error="empty_body"), 400
    if not paypal_verify_webhook_sig(request.headers, body_text):
        return jsonify(ok=False, error="verification_failed"), 400

    try:
        event = json.loads(body_text)
    except Exception:
        return jsonify(ok=False, error="invalid_json"), 400

    event_type = (event.get("event_type") or "").upper()
    resource = event.get("resource") or {}
    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    sub_id  = (resource.get("id") or "").strip() or (resource.get("billing_agreement_id") or "").strip()
    plan_id = (resource.get("plan_id") or "").strip()
    plan_value = REVERSE_PLAN_MAP.get(plan_id)

    try:
        if event_type == "BILLING.SUBSCRIPTION.ACTIVATED":
            status = (resource.get("status") or "ACTIVE").upper()
            if sub_id:
                c = db()
                c.execute("""INSERT OR REPLACE INTO subscriptions(login_email, plan_value, subscription_id, status, created_at, tenant_id)
                             VALUES(?,?,?,?,?,?)""",
                          (AUTH_EMAIL, plan_value or (plan_id or ""), sub_id, status, datetime.now(timezone.utc).isoformat(), current_tenant()["slug"]))
                c.commit(); c.close()
            try:
                plan_label = {"0.5":"0,5 TB","1":"1 TB","2":"2 TB","5":"5 TB"}.get(plan_value, plan_id or "(onbekend plan)")
                body = (
                    "PayPal abonnement geactiveerd:\n\n"
                    f"- Event: {event_type}\n"
                    f"- Subscription ID: {sub_id or '-'}\n"
                    f"- Plan: {plan_label}\n"
                    f"- Datum/tijd (UTC): {now_utc}\n"
                )
                send_email(MAIL_TO, "PayPal: abonnement geactiveerd", body)
            except Exception:
                log.exception("Webhook mail (activated) failed")

        elif event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.RE-ACTIVATED"}:
            new_status = "ACTIVE" if event_type.endswith("RE-ACTIVATED") else event_type.split(".")[-1]
            if sub_id:
                c = db()
                c.execute("UPDATE subscriptions SET status=? WHERE subscription_id=?", (new_status, sub_id))
                c.commit(); c.close()
            try:
                body = (
                    "PayPal abonnementsstatus gewijzigd:\n\n"
                    f"- Event: {event_type}\n"
                    f"- Subscription ID: {sub_id or '-'}\n"
                    f"- Plan ID: {plan_id or '-'}\n"
                    f"- Datum/tijd (UTC): {now_utc}\n"
                )
                send_email(MAIL_TO, f"PayPal: {event_type}", body)
            except Exception:
                log.exception("Webhook mail (status change) failed")

        elif event_type == "PAYMENT.CAPTURE.COMPLETED":
            # Mail bij elke (terugkerende) betaling
            amount = (resource.get("amount") or {}).get("value")
            currency = (resource.get("amount") or {}).get("currency_code")
            try:
                body = (
                    "PayPal betaling ontvangen:\n\n"
                    f"- Event: {event_type}\n"
                    f"- Bedrag: {amount or '-'} {currency or ''}\n"
                    f"- Subscription (indien bekend): {sub_id or '-'}\n"
                    f"- Datum/tijd (UTC): {now_utc}\n"
                )
                send_email(MAIL_TO, "PayPal: betaling ontvangen", body)
            except Exception:
                log.exception("Webhook mail (payment) failed")
        else:
            log.info("PayPal webhook: event %s genegeerd", event_type)

    except Exception:
        log.exception("Webhook processing error")
        return jsonify(ok=False, error="processing_error"), 500

    return jsonify(ok=True)

# Healthcheck & Aliassen
@app.route("/health")
@app.route("/__health")
def health_basic():
    return {"ok": True, "service": "minitransfer", "tenant": "oldehanter"}

@app.route("/health-s3")
def health():
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        return {"ok": True, "bucket": S3_BUCKET}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

@app.route("/package/<token>")
def package_alias(token): return redirect(url_for("package_page", token=token))
@app.route("/stream/<token>/<int:item_id>")
def stream_file_alias(token, item_id): return redirect(url_for("stream_file", token=token, item_id=item_id))
@app.route("/streamzip/<token>")
def stream_zip_alias(token): return redirect(url_for("stream_zip", token=token))



@app.errorhandler(400)
def handle_400(err):
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="bad_request"), 400
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>400</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>400</h1><p>Het verzoek kon niet worden verwerkt.</p><p><a class="btn" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON), 400

@app.errorhandler(401)
def handle_401(err):
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="unauthorized"), 401
    return redirect(url_for("login"))

@app.errorhandler(404)
def handle_404(err):
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>404</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>404</h1><p>Deze pagina of download bestaat niet (meer).</p><p><a class="btn" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON), 404

@app.errorhandler(413)
def handle_413(err):
    return jsonify(ok=False, error="payload_too_large", max_bytes=app.config.get("MAX_CONTENT_LENGTH")), 413

@app.errorhandler(500)
def handle_500(err):
    log.exception("Unhandled server error", exc_info=err)
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="server_error", request_id=getattr(g, "request_id", None)), 500
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>500</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>500</h1><p>Er ging iets mis op de server.</p><p>Referentie: <code>{{ request_id }}</code></p><p><a class="btn" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON, request_id=getattr(g, "request_id", "-")), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
