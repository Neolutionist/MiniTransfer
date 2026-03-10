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
    session, jsonify, Response, stream_with_context
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
AUTH_PASSWORD = "Hulsmaat"  # vast wachtwoord voor het inloggen

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
app.config["SECRET_KEY"] = "olde-hanter-simple-secret"

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
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Downloadlink verlopen</title>
  {{ head_icon|safe }}
  <style>
    {{ base_css }}

    :root{
      --neon1:#ff00a8;
      --neon2:#00f7ff;
      --neon3:#ffe600;
      --neon4:#8a2eff;
      --bg1:#0c0016;
      --bg2:#190028;
      --bg3:#001a35;
      --glass:rgba(18,12,40,.62);
      --line:rgba(255,255,255,.14);
      --txt:#ffffff;
    }

    html,body{height:100%}
    body{
      margin:0;
      color:var(--txt);
      overflow:hidden;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      background:
        radial-gradient(circle at 15% 18%, rgba(255,0,168,.28), transparent 20%),
        radial-gradient(circle at 82% 24%, rgba(0,247,255,.22), transparent 24%),
        radial-gradient(circle at 50% 85%, rgba(255,230,0,.16), transparent 22%),
        linear-gradient(135deg, var(--bg1), var(--bg2) 35%, var(--bg3) 70%, #130018 100%);
    }

    .bgfx, .bgfx::before, .bgfx::after{
      position:fixed; inset:-20%;
      content:"";
      pointer-events:none;
      z-index:0;
    }
    .bgfx{
      background:
        conic-gradient(from 0deg,
          rgba(255,0,168,.16),
          rgba(255,230,0,.12),
          rgba(0,247,255,.14),
          rgba(138,46,255,.16),
          rgba(255,94,0,.14),
          rgba(255,0,168,.16));
      filter:blur(54px) saturate(1.6);
      mix-blend-mode:screen;
      animation:spinGlow 22s linear infinite;
    }
    .bgfx::before{
      background:
        repeating-radial-gradient(circle at center,
          rgba(255,255,255,.05) 0 10px,
          transparent 10px 26px);
      opacity:.24;
      animation:pulseRings 10s ease-in-out infinite;
    }
    .bgfx::after{
      background:
        linear-gradient(90deg,
          rgba(255,0,168,.08),
          rgba(0,247,255,.08),
          rgba(255,230,0,.08),
          rgba(138,46,255,.08),
          rgba(255,0,168,.08));
      background-size:300% 300%;
      mix-blend-mode:overlay;
      animation:driftColors 12s ease-in-out infinite;
    }

    .page{
      position:relative;
      z-index:2;
      height:100%;
      display:grid;
      grid-template-rows:auto 1fr auto;
      gap:14px;
      padding:14px;
      box-sizing:border-box;
    }

    .topbar{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      flex-wrap:wrap;
      padding:10px 14px;
      border:1px solid var(--line);
      border-radius:18px;
      background:var(--glass);
      backdrop-filter:blur(12px) saturate(1.3);
      box-shadow:0 0 22px rgba(255,0,168,.12), 0 0 40px rgba(0,247,255,.08);
    }

    .title{
      margin:0;
      font-size:clamp(1.4rem,3.8vw,2.4rem);
      line-height:1.05;
      font-weight:900;
      text-transform:uppercase;
      letter-spacing:.03em;
      text-shadow:
        0 0 8px var(--neon1),
        0 0 18px var(--neon1),
        0 0 28px var(--neon2),
        0 0 44px var(--neon4);
    }

    .top-actions{
      display:flex;
      align-items:center;
      gap:10px;
      flex-wrap:wrap;
    }

    .chip{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:.72rem .95rem;
      border-radius:999px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.06);
      color:#fff;
      font-weight:800;
      text-shadow:0 0 10px rgba(255,255,255,.08);
      white-space:nowrap;
    }

    .btn-neon{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      gap:.5rem;
      padding:.82rem 1.15rem;
      border-radius:999px;
      border:0;
      text-decoration:none;
      cursor:pointer;
      color:#fff;
      font-weight:900;
      letter-spacing:.04em;
      text-transform:uppercase;
      background:linear-gradient(90deg,#ff00a8,#8a2eff,#00f7ff,#ffe600,#ff00a8);
      background-size:280% 280%;
      box-shadow:0 0 18px rgba(255,0,168,.24), 0 0 34px rgba(0,247,255,.18);
      animation:rainbowMove 5s linear infinite;
      transition:transform .15s ease, filter .15s ease;
    }
    .btn-neon:hover{transform:translateY(-1px) scale(1.02); filter:brightness(1.08)}

    .main{
      display:grid;
      grid-template-columns:minmax(280px,340px) 1fr;
      gap:14px;
      min-height:0;
    }

    .side{
      min-height:0;
      display:flex;
      flex-direction:column;
      gap:14px;
    }

    .panel{
      border:1px solid var(--line);
      border-radius:22px;
      background:var(--glass);
      backdrop-filter:blur(14px) saturate(1.3);
      box-shadow:
        0 0 24px rgba(255,0,168,.10),
        0 0 44px rgba(0,247,255,.08),
        inset 0 0 24px rgba(255,255,255,.03);
      overflow:hidden;
    }

    .panel-h{
      padding:14px 16px;
      border-bottom:1px solid rgba(255,255,255,.08);
      font-weight:900;
      text-transform:uppercase;
      letter-spacing:.12em;
      text-shadow:0 0 10px rgba(255,0,168,.18);
    }

    .panel-b{padding:14px 16px}

    .contact-name{
      margin:0;
      font-size:1.3rem;
      font-weight:900;
      text-shadow:0 0 10px rgba(255,255,255,.12), 0 0 18px rgba(0,247,255,.10);
    }

    .mail{
      display:inline-block;
      margin-top:.65rem;
      padding:.62rem .9rem;
      border-radius:999px;
      text-decoration:none;
      color:#0a0014;
      font-weight:900;
      background:linear-gradient(90deg,#00f7ff,#ffe600,#ff4dd2);
      box-shadow:0 0 16px rgba(0,247,255,.18);
    }

    .small{
      color:rgba(255,255,255,.82);
      line-height:1.55;
    }

    .hudlist{
      display:grid;
      gap:10px;
    }

    .hudrow{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      padding:.65rem .8rem;
      border-radius:14px;
      background:rgba(255,255,255,.05);
      border:1px solid rgba(255,255,255,.08);
      font-weight:800;
    }

    .hudrow .v{
      color:#fff;
      text-shadow:0 0 10px rgba(255,0,168,.16),0 0 16px rgba(0,247,255,.12);
    }

    .help{
      display:grid;
      gap:8px;
      font-size:.96rem;
      color:rgba(255,255,255,.86);
    }

    .game-wrap{
      min-width:0;
      min-height:0;
      display:grid;
      grid-template-rows:1fr auto;
      gap:12px;
    }

    .screen-shell{
      position:relative;
      min-height:0;
      border:1px solid var(--line);
      border-radius:24px;
      overflow:hidden;
      background:rgba(0,0,0,.28);
      box-shadow:
        0 0 30px rgba(255,0,168,.12),
        0 0 54px rgba(0,247,255,.08),
        inset 0 0 36px rgba(255,255,255,.03);
    }

    canvas{
      display:block;
      width:100%;
      height:100%;
      min-height:420px;
      background:#000;
    }

    .overlay-msg{
      position:absolute;
      inset:0;
      display:flex;
      align-items:center;
      justify-content:center;
      text-align:center;
      padding:24px;
      background:linear-gradient(180deg, rgba(10,5,25,.18), rgba(10,5,25,.34));
      pointer-events:none;
    }

    .overlay-card{
      max-width:640px;
      padding:1.2rem 1.3rem;
      border-radius:20px;
      background:rgba(12,8,28,.56);
      border:1px solid rgba(255,255,255,.12);
      backdrop-filter:blur(10px);
      box-shadow:0 0 20px rgba(255,0,168,.10), 0 0 40px rgba(0,247,255,.08);
    }

    .overlay-card h2{
      margin:.1rem 0 .6rem 0;
      font-size:clamp(1.4rem,3vw,2rem);
      text-transform:uppercase;
      letter-spacing:.04em;
      text-shadow:0 0 8px var(--neon1), 0 0 16px var(--neon2);
    }

    .overlay-card p{
      margin:.4rem 0;
      line-height:1.5;
      color:rgba(255,255,255,.92);
    }

    .footer{
      text-align:center;
      color:rgba(255,255,255,.72);
      text-shadow:0 0 10px rgba(255,255,255,.05);
      font-size:.95rem;
    }

    @keyframes spinGlow{
      from{transform:rotate(0deg) scale(1)}
      to{transform:rotate(360deg) scale(1.06)}
    }
    @keyframes pulseRings{
      0%,100%{transform:scale(1); opacity:.22}
      50%{transform:scale(1.08); opacity:.36}
    }
    @keyframes driftColors{
      0%,100%{background-position:0% 50%}
      50%{background-position:100% 50%}
    }
    @keyframes rainbowMove{
      0%{background-position:0% 50%}
      100%{background-position:200% 50%}
    }

    @media (max-width: 980px){
      .main{grid-template-columns:1fr}
      .side{order:2}
      .game-wrap{order:1}
      canvas{min-height:360px}
    }

    @media (max-width: 640px){
      .page{padding:10px}
      .topbar{padding:10px 12px}
      .panel-b{padding:12px 14px}
      canvas{min-height:300px}
    }

    @media (prefers-reduced-motion: reduce){
      .bgfx,.bgfx::before,.bgfx::after,.btn-neon{animation:none !important}
    }
  </style>
</head>
<body>
  <div class="bgfx"></div>

  <div class="page">
    <div class="topbar">
      <h1 class="title">Downloadlink verlopen</h1>
      <div class="top-actions">
        <div class="chip">Retro 3D Shooter • 2 Levels • Projectiles • Barrels</div>
        <a class="btn-neon" href="/">Terug naar website</a>
      </div>
    </div>

    <div class="main">
      <div class="side">
        <div class="panel">
          <div class="panel-h">Contact</div>
          <div class="panel-b">
            <p class="small" style="margin-top:0">
              Deze downloadlink is helaas verlopen of niet meer beschikbaar.
              Neem contact op voor een nieuwe link.
            </p>
            <p class="contact-name">Patrick Lankhorst</p>
            <a class="mail" href="mailto:Patrick@oldehanter.nl">Patrick@oldehanter.nl</a>
          </div>
        </div>

        <div class="panel">
          <div class="panel-h">Game HUD</div>
          <div class="panel-b">
            <div class="hudlist">
              <div class="hudrow"><span>Health</span><span class="v" id="hudHealth">100</span></div>
              <div class="hudrow"><span>Score</span><span class="v" id="hudScore">0</span></div>
              <div class="hudrow"><span>Ammo</span><span class="v" id="hudAmmo">24</span></div>
              <div class="hudrow"><span>Enemies</span><span class="v" id="hudEnemies">0</span></div>
            </div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-h">Controls</div>
          <div class="panel-b help">
            <div><strong>W A S D</strong> bewegen</div>
            <div><strong>Muis</strong> kijken</div>
            <div><strong>Spatie</strong> schieten</div>
            <div><strong>Explosive barrels</strong> kunnen kettingreacties veroorzaken</div>
            <div><strong>Portal</strong> opent zodra alle vijanden weg zijn</div>
            <div><strong>R</strong> herstarten</div>
            <div><strong>Klik op het scherm</strong> om de muis te locken</div>
          </div>
        </div>
      </div>

      <div class="game-wrap">
        <div class="screen-shell">
          <canvas id="game"></canvas>
          <div class="overlay-msg" id="overlay">
            <div class="overlay-card">
              <h2>Neon Panic 3D</h2>
              <p>Een originele retro boomer-shooter demo in psychedelische stijl.</p>
              <p>Klik in het scherm om te starten. Beweeg met WASD, kijk met de muis en schiet met spatie.</p>
              <p style="margin-bottom:0">Druk op <strong>R</strong> om opnieuw te beginnen.</p>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</div>
  </div>

<script>
  const canvas = document.getElementById('game');
  const ctx = canvas.getContext('2d');
  const overlay = document.getElementById('overlay');

  const hudHealth = document.getElementById('hudHealth');
  const hudScore = document.getElementById('hudScore');
  const hudAmmo = document.getElementById('hudAmmo');
  const hudEnemies = document.getElementById('hudEnemies');

  function resizeCanvas(){
    const rect = canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(rect.width * dpr);
    canvas.height = Math.floor(rect.height * dpr);
    ctx.setTransform(dpr,0,0,dpr,0,0);
  }
  window.addEventListener('resize', resizeCanvas);
  resizeCanvas();

  const LEVELS = [
    {
      map: [
        1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,
        1,0,0,0,0,0,0,0,1,0,0,0,0,0,0,1,
        1,0,1,1,1,0,1,0,1,0,1,1,1,1,0,1,
        1,0,1,0,0,0,1,0,0,0,1,0,0,1,0,1,
        1,0,1,0,1,1,1,1,1,0,1,0,0,1,0,1,
        1,0,0,0,0,0,0,0,1,0,1,0,3,0,0,1,
        1,0,1,1,1,1,1,0,1,0,1,1,1,1,0,1,
        1,0,0,0,0,0,1,0,0,0,0,0,0,1,0,1,
        1,1,1,1,1,0,1,1,1,1,1,1,0,1,0,1,
        1,0,0,0,1,0,0,0,0,0,0,1,0,1,0,1,
        1,0,1,0,1,1,1,1,1,1,0,1,0,1,0,1,
        1,0,1,0,0,0,0,0,0,1,0,0,0,1,0,1,
        1,0,1,1,1,1,1,1,0,1,1,1,0,1,0,1,
        1,0,0,0,0,0,0,1,0,0,0,1,0,0,0,1,
        1,0,0,0,0,0,0,0,0,1,0,0,0,0,2,1,
        1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1
      ],
      player: {x:1.5,y:1.5,ang:0},
      enemies: [
        {x:5.5,y:5.5,hp:3,phase:0,type:'orb'},
        {x:9.5,y:3.5,hp:3,phase:1,type:'orb'},
        {x:13.5,y:7.5,hp:4,phase:2,type:'orb'},
        {x:10.5,y:11.5,hp:4,phase:3,type:'orb'},
        {x:4.5,y:13.5,hp:5,phase:4,type:'orb'}
      ],
      pickups: [
        {x:3.5,y:5.5,type:'ammo',alive:true},
        {x:12.5,y:13.5,type:'ammo',alive:true},
        {x:8.5,y:7.5,type:'med',alive:true}
      ],
      barrels: [
        {x:12.5,y:5.5,alive:true},
        {x:7.5,y:7.5,alive:true}
      ]
    },
    {
      map: [
        1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,
        1,0,0,0,0,0,0,0,0,1,0,0,0,0,0,1,
        1,0,1,1,1,1,1,1,0,1,0,1,1,1,0,1,
        1,0,1,0,0,0,0,1,0,1,0,1,0,0,0,1,
        1,0,1,0,1,1,0,1,0,0,0,1,0,1,1,1,
        1,0,0,0,1,0,0,1,1,1,0,1,0,0,0,1,
        1,1,1,0,1,0,3,0,0,1,0,1,1,1,0,1,
        1,0,0,0,1,1,1,1,0,1,0,0,0,1,0,1,
        1,0,1,0,0,0,0,1,0,1,1,1,0,1,0,1,
        1,0,1,1,1,1,0,1,0,0,0,1,0,1,0,1,
        1,0,0,0,0,1,0,1,1,1,0,1,0,1,0,1,
        1,0,1,1,0,1,0,0,0,1,0,0,0,1,0,1,
        1,0,1,0,0,1,1,1,0,1,1,1,0,1,0,1,
        1,0,0,0,1,0,0,0,0,0,0,1,0,0,2,1,
        1,0,0,0,1,0,0,0,0,0,0,1,0,0,0,1,
        1,1,1,1,1,1,1,1,1,1,1,1,1,1,1,1
      ],
      player: {x:1.5,y:1.5,ang:0},
      enemies: [
        {x:7.5,y:6.5,hp:5,phase:0,type:'orb'},
        {x:10.5,y:4.5,hp:4,phase:1,type:'orb'},
        {x:3.5,y:10.5,hp:4,phase:2,type:'orb'},
        {x:12.5,y:12.5,hp:6,phase:3,type:'orb'},
        {x:13.5,y:3.5,hp:5,phase:4,type:'orb'},
        {x:8.5,y:13.5,hp:6,phase:5,type:'orb'}
      ],
      pickups: [
        {x:6.5,y:6.5,type:'ammo',alive:true},
        {x:11.5,y:11.5,type:'med',alive:true},
        {x:3.5,y:13.5,type:'ammo',alive:true}
      ],
      barrels: [
        {x:6.5,y:6.5,alive:true},
        {x:9.5,y:9.5,alive:true},
        {x:12.5,y:5.5,alive:true}
      ]
    }
  ];

  let MAP_W = 16, MAP_H = 16, MAP = LEVELS[0].map.slice();
  let state;

  const input = {
    mouseLocked: false,
    lastShot: 0
  };

  let audioCtx = null;
  function ensureAudio(){
    if(!audioCtx){
      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    }
    if(audioCtx.state === 'suspended'){
      audioCtx.resume();
    }
  }

  function beep(freq=440, duration=0.08, type='square', vol=0.04, slide=0){
    if(!audioCtx) return;
    const now = audioCtx.currentTime;
    const o = audioCtx.createOscillator();
    const g = audioCtx.createGain();
    o.type = type;
    o.frequency.setValueAtTime(freq, now);
    if(slide){
      o.frequency.linearRampToValueAtTime(freq + slide, now + duration);
    }
    g.gain.setValueAtTime(vol, now);
    g.gain.exponentialRampToValueAtTime(0.0001, now + duration);
    o.connect(g).connect(audioCtx.destination);
    o.start(now);
    o.stop(now + duration);
  }

  function noiseBurst(duration=0.08, vol=0.03){
    if(!audioCtx) return;
    const buffer = audioCtx.createBuffer(1, audioCtx.sampleRate * duration, audioCtx.sampleRate);
    const data = buffer.getChannelData(0);
    for(let i=0;i<data.length;i++) data[i] = (Math.random() * 2 - 1) * (1 - i/data.length);
    const src = audioCtx.createBufferSource();
    const g = audioCtx.createGain();
    src.buffer = buffer;
    g.gain.value = vol;
    src.connect(g).connect(audioCtx.destination);
    src.start();
  }

  function applyLevel(levelIndex, keepStats=false){
    const lvl = LEVELS[levelIndex];
    MAP = lvl.map.slice();
    const baseHealth = keepStats && state ? state.health : 100;
    const baseScore  = keepStats && state ? state.score : 0;
    const baseAmmo   = keepStats && state ? Math.max(state.ammo, 18) : 24;

    state = {
      level: levelIndex,
      x: lvl.player.x,
      y: lvl.player.y,
      ang: lvl.player.ang,
      fov: Math.PI / 3,
      health: baseHealth,
      score: baseScore,
      ammo: baseAmmo,
      fireCooldown: 0,
      hurtFlash: 0,
      won: false,
      dead: false,
      keys: Object.create(null),
      enemies: lvl.enemies.map(e => ({...e, shootCd: 0.8 + Math.random() * 1.4})),
      pickups: lvl.pickups.map(p => ({...p})),
      barrels: lvl.barrels.map(b => ({...b, exploding:0})),
      projectiles: [],
      explosions: [],
      portalOpen: false
    };

    overlay.style.display = 'flex';
    overlay.querySelector('.overlay-card').innerHTML =
      '<h2>Level ' + (levelIndex + 1) + '</h2><p>Klik in het scherm om verder te gaan.</p><p>Schakel vijanden uit, ontwijk projectielen en bereik de portal.</p>';
    updateHUD();
  }

  function resetGame(){
    applyLevel(0, false);
  }

  function cell(x,y){
    if(x < 0 || y < 0 || x >= MAP_W || y >= MAP_H) return 1;
    return MAP[y * MAP_W + x];
  }

  function setCell(x,y,val){
    if(x < 0 || y < 0 || x >= MAP_W || y >= MAP_H) return;
    MAP[y * MAP_W + x] = val;
  }

  function normalizeAngle(a){
    while(a > Math.PI) a -= Math.PI * 2;
    while(a < -Math.PI) a += Math.PI * 2;
    return a;
  }

  function hasLineOfSight(x1,y1,x2,y2){
    const dx = x2 - x1, dy = y2 - y1;
    const dist = Math.hypot(dx,dy);
    const steps = Math.ceil(dist * 12);
    for(let i=1; i<steps; i++){
      const t = i / steps;
      const x = x1 + dx * t;
      const y = y1 + dy * t;
      const c = cell(Math.floor(x), Math.floor(y));
      if(c === 1) return false;
    }
    return true;
  }

  function updateHUD(){
    hudHealth.textContent = Math.max(0, Math.round(state.health));
    hudScore.textContent = state.score;
    hudAmmo.textContent = state.ammo;
    hudEnemies.textContent = state.enemies.filter(e => e.hp > 0).length;
  }

  function tryMove(nx, ny){
    const r = 0.18;
    if(cell(Math.floor(nx - r), Math.floor(state.y)) !== 1 && cell(Math.floor(nx + r), Math.floor(state.y)) !== 1){
      state.x = nx;
    }
    if(cell(Math.floor(state.x), Math.floor(ny - r)) !== 1 && cell(Math.floor(state.x), Math.floor(ny + r)) !== 1){
      state.y = ny;
    }
  }

  document.addEventListener('keydown', (e) => {
    state.keys[e.key.toLowerCase()] = true;
    if(e.code === 'Space') e.preventDefault();
    if(e.key.toLowerCase() === 'r') resetGame();
  });

  document.addEventListener('keyup', (e) => {
    state.keys[e.key.toLowerCase()] = false;
  });

  canvas.addEventListener('click', async () => {
    ensureAudio();
    try { await canvas.requestPointerLock(); } catch(e) {}
    overlay.style.display = 'none';
  });

  document.addEventListener('pointerlockchange', () => {
    input.mouseLocked = document.pointerLockElement === canvas;
  });

  document.addEventListener('mousemove', (e) => {
    if(!input.mouseLocked || state.dead || state.won) return;
    state.ang += e.movementX * 0.0025;
  });

  document.addEventListener('keydown', (e) => {
    if(e.code === 'Space') shoot();
  });

  function nearestBarrelInSight(){
    let best = null;
    let bestDist = Infinity;
    for(const b of state.barrels){
      if(!b.alive) continue;
      const dx = b.x - state.x;
      const dy = b.y - state.y;
      const dist = Math.hypot(dx,dy);
      const angTo = Math.atan2(dy, dx);
      const diff = normalizeAngle(angTo - state.ang);
      if(Math.abs(diff) > 0.11) continue;
      if(!hasLineOfSight(state.x, state.y, b.x, b.y)) continue;
      if(dist < bestDist){
        bestDist = dist;
        best = b;
      }
    }
    return best;
  }

  function nearestEnemyInSight(){
    let best = null;
    let bestDist = Infinity;
    for(const enemy of state.enemies){
      if(enemy.hp <= 0) continue;
      const dx = enemy.x - state.x;
      const dy = enemy.y - state.y;
      const dist = Math.hypot(dx, dy);
      const angTo = Math.atan2(dy, dx);
      const diff = normalizeAngle(angTo - state.ang);
      if(Math.abs(diff) > 0.12) continue;
      if(!hasLineOfSight(state.x, state.y, enemy.x, enemy.y)) continue;
      if(dist < bestDist){
        bestDist = dist;
        best = enemy;
      }
    }
    return best;
  }

  function shoot(){
    const now = performance.now();
    if(now - input.lastShot < 220) return;
    if(state.dead || state.won) return;
    if(state.ammo <= 0) return;

    ensureAudio();
    state.ammo--;
    input.lastShot = now;
    state.fireCooldown = 0.08;
    beep(170, 0.06, 'square', 0.045, 180);
    noiseBurst(0.04, 0.02);

    const barrel = nearestBarrelInSight();
    if(barrel){
      explodeBarrel(barrel);
      updateHUD();
      return;
    }

    const best = nearestEnemyInSight();
    if(best){
      best.hp -= 1;
      if(best.hp <= 0){
        state.score += 100;
        beep(260, 0.08, 'sawtooth', 0.035, -100);
      } else {
        state.score += 20;
      }
    }

    updateHUD();
  }

  function explodeBarrel(barrel){
    if(!barrel.alive) return;
    barrel.alive = false;
    state.explosions.push({x:barrel.x, y:barrel.y, t:0.45, r:2.2});
    beep(120, 0.18, 'sawtooth', 0.05, -90);
    noiseBurst(0.12, 0.04);

    for(const enemy of state.enemies){
      if(enemy.hp <= 0) continue;
      const d = Math.hypot(enemy.x - barrel.x, enemy.y - barrel.y);
      if(d < 2.2){
        enemy.hp -= d < 1.15 ? 10 : 3;
        if(enemy.hp <= 0) state.score += 120;
      }
    }

    const dp = Math.hypot(state.x - barrel.x, state.y - barrel.y);
    if(dp < 2.1){
      state.health -= dp < 1.1 ? 45 : 18;
      state.hurtFlash = 0.3;
    }

    for(const other of state.barrels){
      if(other.alive){
        const d = Math.hypot(other.x - barrel.x, other.y - barrel.y);
        if(d < 1.9){
          setTimeout(() => explodeBarrel(other), 80);
        }
      }
    }
  }

  function enemyShoot(enemy){
    const dx = state.x - enemy.x;
    const dy = state.y - enemy.y;
    const dist = Math.hypot(dx,dy);
    if(dist < 0.001) return;
    const vx = dx / dist * 2.8;
    const vy = dy / dist * 2.8;

    state.projectiles.push({
      x: enemy.x,
      y: enemy.y,
      vx, vy,
      from: 'enemy',
      life: 2.6
    });

    beep(520, 0.04, 'triangle', 0.02, -160);
  }

  function openPortalIfClear(){
    const alive = state.enemies.filter(e => e.hp > 0).length;
    if(alive === 0) state.portalOpen = true;
  }

  function nextLevel(){
    if(state.level + 1 < LEVELS.length){
      applyLevel(state.level + 1, true);
    } else {
      state.won = true;
      overlay.style.display = 'flex';
      overlay.querySelector('.overlay-card').innerHTML =
        '<h2>Victory</h2><p>Je hebt alle neon levels overleefd.</p><p>Score: <strong>' + state.score + '</strong></p><p>Druk op <strong>R</strong> om opnieuw te spelen.</p>';
      beep(440, 0.1, 'square', 0.03, 120);
      setTimeout(() => beep(660, 0.12, 'square', 0.03, 80), 120);
      setTimeout(() => beep(880, 0.14, 'square', 0.03, 40), 240);
    }
  }

  function castRay(angle){
    const sin = Math.sin(angle);
    const cos = Math.cos(angle);

    let mapX = Math.floor(state.x);
    let mapY = Math.floor(state.y);

    const deltaDistX = Math.abs(1 / (cos || 0.00001));
    const deltaDistY = Math.abs(1 / (sin || 0.00001));

    let stepX, sideDistX;
    let stepY, sideDistY;

    if(cos < 0){
      stepX = -1;
      sideDistX = (state.x - mapX) * deltaDistX;
    } else {
      stepX = 1;
      sideDistX = (mapX + 1 - state.x) * deltaDistX;
    }

    if(sin < 0){
      stepY = -1;
      sideDistY = (state.y - mapY) * deltaDistY;
    } else {
      stepY = 1;
      sideDistY = (mapY + 1 - state.y) * deltaDistY;
    }

    let hit = 0;
    let side = 0;
    let hitType = 1;

    while(!hit){
      if(sideDistX < sideDistY){
        sideDistX += deltaDistX;
        mapX += stepX;
        side = 0;
      } else {
        sideDistY += deltaDistY;
        mapY += stepY;
        side = 1;
      }
      hitType = cell(mapX, mapY);
      if(hitType !== 0) hit = 1;
    }

    let perpWallDist;
    if(side === 0){
      perpWallDist = (mapX - state.x + (1 - stepX) / 2) / (cos || 0.00001);
    } else {
      perpWallDist = (mapY - state.y + (1 - stepY) / 2) / (sin || 0.00001);
    }

    let wallX;
    if(side === 0){
      wallX = state.y + perpWallDist * sin;
    } else {
      wallX = state.x + perpWallDist * cos;
    }
    wallX -= Math.floor(wallX);

    return {dist: Math.max(0.0001, perpWallDist), side, hitType, wallX};
  }

  function update(dt){
    if(state.dead || state.won) return;

    const move = 2.7 * dt;
    const rot = 1.9 * dt;

    if(state.keys['arrowleft']) state.ang -= rot;
    if(state.keys['arrowright']) state.ang += rot;

    let mx = 0, my = 0;
    const cos = Math.cos(state.ang), sin = Math.sin(state.ang);
    const strafeCos = Math.cos(state.ang + Math.PI/2), strafeSin = Math.sin(state.ang + Math.PI/2);

    if(state.keys['w']) { mx += cos * move; my += sin * move; }
    if(state.keys['s']) { mx -= cos * move; my -= sin * move; }
    if(state.keys['a']) { mx -= strafeCos * move; my -= strafeSin * move; }
    if(state.keys['d']) { mx += strafeCos * move; my += strafeSin * move; }

    tryMove(state.x + mx, state.y + my);

    for(const p of state.pickups){
      if(!p.alive) continue;
      const d = Math.hypot(p.x - state.x, p.y - state.y);
      if(d < 0.45){
        p.alive = false;
        if(p.type === 'ammo'){
          state.ammo += 8;
          beep(800, 0.05, 'triangle', 0.025, 80);
        }
        if(p.type === 'med'){
          state.health = Math.min(100, state.health + 30);
          beep(600, 0.07, 'sine', 0.03, 120);
        }
      }
    }

    for(const e of state.enemies){
      if(e.hp <= 0) continue;
      const dx = state.x - e.x;
      const dy = state.y - e.y;
      const dist = Math.hypot(dx, dy);
      e.phase += dt * 4;
      e.shootCd -= dt;

      if(dist > 0.95 && hasLineOfSight(e.x, e.y, state.x, state.y)){
        const vx = dx / dist * dt * 1.05;
        const vy = dy / dist * dt * 1.05;
        const nx = e.x + vx;
        const ny = e.y + vy;
        if(cell(Math.floor(nx), Math.floor(e.y)) !== 1) e.x = nx;
        if(cell(Math.floor(e.x), Math.floor(ny)) !== 1) e.y = ny;
      }

      if(dist < 0.9){
        state.health -= 18 * dt;
        state.hurtFlash = 0.18;
      }

      if(dist < 6.5 && hasLineOfSight(e.x, e.y, state.x, state.y) && e.shootCd <= 0){
        enemyShoot(e);
        e.shootCd = 1.0 + Math.random() * 1.8;
      }
    }

    for(let i = state.projectiles.length - 1; i >= 0; i--){
      const p = state.projectiles[i];
      p.life -= dt;
      p.x += p.vx * dt;
      p.y += p.vy * dt;

      if(p.life <= 0 || cell(Math.floor(p.x), Math.floor(p.y)) === 1){
        state.projectiles.splice(i, 1);
        continue;
      }

      for(const b of state.barrels){
        if(!b.alive) continue;
        if(Math.hypot(p.x - b.x, p.y - b.y) < 0.28){
          explodeBarrel(b);
          state.projectiles.splice(i, 1);
          p.life = 0;
          break;
        }
      }
      if(p.life <= 0) continue;

      if(Math.hypot(p.x - state.x, p.y - state.y) < 0.24){
        state.health -= 12;
        state.hurtFlash = 0.22;
        state.projectiles.splice(i, 1);
        beep(180, 0.08, 'sawtooth', 0.03, -80);
      }
    }

    for(let i = state.explosions.length - 1; i >= 0; i--){
      state.explosions[i].t -= dt;
      if(state.explosions[i].t <= 0) state.explosions.splice(i, 1);
    }

    if(state.hurtFlash > 0) state.hurtFlash -= dt;
    if(state.fireCooldown > 0) state.fireCooldown -= dt;

    openPortalIfClear();

    if(state.portalOpen && cell(Math.floor(state.x), Math.floor(state.y)) === 2){
      nextLevel();
      return;
    }

    if(state.health <= 0){
      state.dead = true;
      overlay.style.display = 'flex';
      overlay.querySelector('.overlay-card').innerHTML =
        '<h2>Game Over</h2><p>Je bent uitgeschakeld in de neon doolhof.</p><p>Druk op <strong>R</strong> om opnieuw te beginnen.</p>';
      beep(140, 0.2, 'sawtooth', 0.04, -100);
    }

    updateHUD();
  }

  function roundRect(ctx, x, y, w, h, r, fill){
    ctx.beginPath();
    ctx.moveTo(x+r, y);
    ctx.arcTo(x+w, y, x+w, y+h, r);
    ctx.arcTo(x+w, y+h, x, y+h, r);
    ctx.arcTo(x, y+h, x, y, r);
    ctx.arcTo(x, y, x+w, y, r);
    if(fill) ctx.fill();
  }

  function drawMiniMap(w,h){
    const size = 110;
    const x0 = w - size - 14;
    const y0 = 14;
    const cs = size / MAP_W;

    ctx.fillStyle = 'rgba(8,8,16,.38)';
    roundRect(ctx, x0-6, y0-6, size+12, size+12, 12, true);

    for(let y=0; y<MAP_H; y++){
      for(let x=0; x<MAP_W; x++){
        const t = cell(x,y);
        if(t === 1) ctx.fillStyle = '#34114f';
        else if(t === 2) ctx.fillStyle = state.portalOpen ? '#ffe600' : '#444400';
        else if(t === 3) ctx.fillStyle = '#5d2600';
        else ctx.fillStyle = 'rgba(255,255,255,.05)';
        ctx.fillRect(x0 + x*cs, y0 + y*cs, cs-1, cs-1);
      }
    }

    for(const p of state.pickups){
      if(!p.alive) continue;
      ctx.fillStyle = p.type === 'ammo' ? '#00f7ff' : '#ffe600';
      ctx.fillRect(x0 + p.x*cs - 2, y0 + p.y*cs - 2, 4, 4);
    }

    for(const b of state.barrels){
      if(!b.alive) continue;
      ctx.fillStyle = '#ff6a00';
      ctx.fillRect(x0 + b.x*cs - 2, y0 + b.y*cs - 2, 5, 5);
    }

    for(const e of state.enemies){
      if(e.hp <= 0) continue;
      ctx.fillStyle = '#ff00a8';
      ctx.beginPath();
      ctx.arc(x0 + e.x*cs, y0 + e.y*cs, 3, 0, Math.PI*2);
      ctx.fill();
    }

    ctx.fillStyle = '#fff';
    ctx.beginPath();
    ctx.arc(x0 + state.x*cs, y0 + state.y*cs, 4, 0, Math.PI*2);
    ctx.fill();

    ctx.strokeStyle = '#00f7ff';
    ctx.beginPath();
    ctx.moveTo(x0 + state.x*cs, y0 + state.y*cs);
    ctx.lineTo(x0 + state.x*cs + Math.cos(state.ang)*10, y0 + state.y*cs + Math.sin(state.ang)*10);
    ctx.stroke();
  }

  function draw(){
    const w = canvas.clientWidth;
    const h = canvas.clientHeight;
    ctx.clearRect(0,0,w,h);

    const sky = ctx.createLinearGradient(0,0,0,h*0.5);
    sky.addColorStop(0,'#25003d');
    sky.addColorStop(0.45,'#4a006d');
    sky.addColorStop(1,'#09214a');
    ctx.fillStyle = sky;
    ctx.fillRect(0,0,w,h*0.52);

    const floor = ctx.createLinearGradient(0,h*0.52,0,h);
    floor.addColorStop(0,'#12081c');
    floor.addColorStop(1,'#05070c');
    ctx.fillStyle = floor;
    ctx.fillRect(0,h*0.52,w,h*0.48);

    const glow = ctx.createLinearGradient(0,h*0.45,0,h*0.62);
    glow.addColorStop(0,'rgba(0,247,255,.04)');
    glow.addColorStop(.5,'rgba(255,0,168,.10)');
    glow.addColorStop(1,'rgba(255,230,0,.03)');
    ctx.fillStyle = glow;
    ctx.fillRect(0,h*0.42,w,h*0.22);

    const zBuffer = new Array(w);

    for(let x=0; x<w; x++){
      const cameraX = 2 * x / w - 1;
      const rayAngle = state.ang + Math.atan(cameraX * Math.tan(state.fov / 2));
      const ray = castRay(rayAngle);
      const dist = ray.dist * Math.cos(rayAngle - state.ang);
      zBuffer[x] = dist;

      const lineH = Math.min(h * 1.9, h / Math.max(0.0001, dist));
      const drawStart = (h - lineH) / 2;

      let c1, c2;
      if(ray.hitType === 2){
        c1 = state.portalOpen ? '#ffe600' : '#665500';
        c2 = state.portalOpen ? '#00f7ff' : '#3c3c00';
      } else if(ray.hitType === 3){
        c1 = '#ff8a00';
        c2 = '#6d1b00';
      } else if(ray.side === 0){
        c1 = '#ff00a8'; c2 = '#8a2eff';
      } else {
        c1 = '#00f7ff'; c2 = '#004bff';
      }

      const wallGrad = ctx.createLinearGradient(0, drawStart, 0, drawStart + lineH);
      wallGrad.addColorStop(0, c1);
      wallGrad.addColorStop(.55, c2);
      wallGrad.addColorStop(1, '#130018');

      const shade = Math.max(0.15, 1 - dist / 10);
      ctx.globalAlpha = shade;
      ctx.fillStyle = wallGrad;
      ctx.fillRect(x, drawStart, 1.2, lineH);

      ctx.globalAlpha = shade * 0.18;
      if(((drawStart|0) + x) % 6 < 3){
        ctx.fillStyle = '#fff';
        ctx.fillRect(x, drawStart, 1, lineH);
      }
      ctx.globalAlpha = 1;
    }

    const sprites = [];

    for(const p of state.pickups){
      if(p.alive) sprites.push({x:p.x, y:p.y, type:p.type});
    }
    for(const e of state.enemies){
      if(e.hp > 0) sprites.push({x:e.x, y:e.y, type:'enemy', hp:e.hp, phase:e.phase});
    }
    for(const b of state.barrels){
      if(b.alive) sprites.push({x:b.x, y:b.y, type:'barrel'});
    }
    for(const p of state.projectiles){
      sprites.push({x:p.x, y:p.y, type:'projectile'});
    }
    for(const ex of state.explosions){
      sprites.push({x:ex.x, y:ex.y, type:'explosion', t:ex.t, r:ex.r});
    }

    sprites.sort((a,b) => {
      const da = (a.x-state.x)**2 + (a.y-state.y)**2;
      const db = (b.x-state.x)**2 + (b.y-state.y)**2;
      return db - da;
    });

    for(const s of sprites){
      const dx = s.x - state.x;
      const dy = s.y - state.y;
      const dist = Math.hypot(dx,dy);
      const ang = normalizeAngle(Math.atan2(dy,dx) - state.ang);

      if(Math.abs(ang) > state.fov * 0.75) continue;

      const screenX = (0.5 + ang / state.fov) * w;
      const size = Math.max(12, h / dist * 0.7);
      const left = Math.floor(screenX - size/2);
      const right = Math.floor(screenX + size/2);

      let visible = false;
      for(let x=left; x<right; x++){
        if(x >= 0 && x < w && dist < zBuffer[x] + 0.1){
          visible = true;
          break;
        }
      }
      if(!visible) continue;

      if(s.type === 'enemy'){
        const bob = Math.sin((s.phase || 0) * 2) * 6;
        const r = size * 0.32;
        const cx = screenX;
        const cy = h / 2 - size*0.18 + bob;

        const grad = ctx.createRadialGradient(cx-r*0.25, cy-r*0.25, r*0.2, cx, cy, r*1.3);
        grad.addColorStop(0, '#fff176');
        grad.addColorStop(0.25, '#ff00a8');
        grad.addColorStop(0.6, '#8a2eff');
        grad.addColorStop(1, 'rgba(0,247,255,.1)');
        ctx.fillStyle = grad;
        ctx.shadowColor = '#ff00a8';
        ctx.shadowBlur = 18;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI*2);
        ctx.fill();
        ctx.shadowBlur = 0;

        ctx.fillStyle = '#00f7ff';
        ctx.beginPath(); ctx.arc(cx-r*0.32, cy-r*0.1, r*0.12, 0, Math.PI*2); ctx.fill();
        ctx.beginPath(); ctx.arc(cx+r*0.32, cy-r*0.1, r*0.12, 0, Math.PI*2); ctx.fill();

        const bw = size * 0.7;
        const bx = cx - bw/2;
        const by = cy - r - 14;
        ctx.fillStyle = 'rgba(0,0,0,.35)';
        ctx.fillRect(bx, by, bw, 6);
        ctx.fillStyle = '#ffe600';
        ctx.fillRect(bx, by, bw * Math.max(0, s.hp / 6), 6);
      }
      else if(s.type === 'ammo' || s.type === 'med'){
        const cx = screenX;
        const cy = h / 2 + size*0.22;
        const r = size * 0.18;
        const col = s.type === 'ammo' ? '#00f7ff' : '#ffe600';
        ctx.shadowColor = col;
        ctx.shadowBlur = 16;
        ctx.fillStyle = col;
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI*2);
        ctx.fill();
        ctx.shadowBlur = 0;
      }
      else if(s.type === 'barrel'){
        const bw = size * 0.28;
        const bh = size * 0.45;
        const bx = screenX - bw/2;
        const by = h/2 + 8 - bh/2;
        const g = ctx.createLinearGradient(bx, by, bx, by + bh);
        g.addColorStop(0, '#ff8a00');
        g.addColorStop(1, '#5a1200');
        ctx.fillStyle = g;
        roundRect(ctx, bx, by, bw, bh, 6, true);
        ctx.fillStyle = '#222';
        ctx.fillRect(bx, by + bh*0.18, bw, 4);
        ctx.fillRect(bx, by + bh*0.74, bw, 4);
      }
      else if(s.type === 'projectile'){
        const r = Math.max(4, size * 0.08);
        ctx.shadowColor = '#00f7ff';
        ctx.shadowBlur = 18;
        ctx.fillStyle = '#00f7ff';
        ctx.beginPath();
        ctx.arc(screenX, h/2, r, 0, Math.PI*2);
        ctx.fill();
        ctx.shadowBlur = 0;
      }
      else if(s.type === 'explosion'){
        const p = s.t / 0.45;
        const rr = (1 - p) * (size * 0.45 + 30);
        const grd = ctx.createRadialGradient(screenX, h/2, 2, screenX, h/2, rr);
        grd.addColorStop(0, 'rgba(255,255,180,.95)');
        grd.addColorStop(0.25, 'rgba(255,230,0,.8)');
        grd.addColorStop(0.55, 'rgba(255,90,0,.55)');
        grd.addColorStop(1, 'rgba(255,0,120,0)');
        ctx.fillStyle = grd;
        ctx.beginPath();
        ctx.arc(screenX, h/2, rr, 0, Math.PI*2);
        ctx.fill();
      }
    }

    const weaponW = Math.min(260, w * 0.34);
    const weaponH = Math.min(180, h * 0.28);
    const wx = w/2 - weaponW/2 + Math.sin(performance.now()/120) * 2;
    const wy = h - weaponH + Math.cos(performance.now()/160) * 2;

    const weap = ctx.createLinearGradient(wx, wy, wx+weaponW, wy+weaponH);
    weap.addColorStop(0, '#ff00a8');
    weap.addColorStop(.5, '#8a2eff');
    weap.addColorStop(1, '#00f7ff');
    ctx.fillStyle = weap;
    ctx.globalAlpha = .9;
    roundRect(ctx, wx, wy + 36, weaponW, weaponH-20, 20, true);
    ctx.globalAlpha = 1;

    ctx.fillStyle = '#130018';
    roundRect(ctx, wx + weaponW*0.42, wy, weaponW*0.16, weaponH*0.72, 12, true);
    ctx.fillStyle = '#ffe600';
    roundRect(ctx, wx + weaponW*0.455, wy + 10, weaponW*0.09, weaponH*0.46, 8, true);

    if(state.fireCooldown > 0){
      ctx.fillStyle = 'rgba(255,230,0,.55)';
      ctx.beginPath();
      ctx.arc(w/2, h*0.66, 24 + Math.random()*10, 0, Math.PI*2);
      ctx.fill();
    }

    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 2;
    ctx.shadowColor = '#00f7ff';
    ctx.shadowBlur = 10;
    ctx.beginPath();
    ctx.moveTo(w/2 - 12, h/2); ctx.lineTo(w/2 - 4, h/2);
    ctx.moveTo(w/2 + 4, h/2); ctx.lineTo(w/2 + 12, h/2);
    ctx.moveTo(w/2, h/2 - 12); ctx.lineTo(w/2, h/2 - 4);
    ctx.moveTo(w/2, h/2 + 4); ctx.lineTo(w/2, h/2 + 12);
    ctx.stroke();
    ctx.shadowBlur = 0;

    drawMiniMap(w,h);

    if(state.hurtFlash > 0){
      ctx.fillStyle = 'rgba(255,0,80,' + Math.min(0.35, state.hurtFlash * 1.6) + ')';
      ctx.fillRect(0,0,w,h);
    }

    ctx.fillStyle = 'rgba(10,5,25,.35)';
    ctx.fillRect(10, 10, 310, 36);
    ctx.fillStyle = '#fff';
    ctx.font = '800 14px system-ui, sans-serif';
    ctx.fillText('LVL ' + (state.level + 1), 20, 32);
    ctx.fillText('HEALTH ' + Math.max(0, Math.round(state.health)), 70, 32);
    ctx.fillText('AMMO ' + state.ammo, 170, 32);
    ctx.fillText('SCORE ' + state.score, 240, 32);
  }

  applyLevel(0, false);

  let last = performance.now();
  function loop(now){
    const dt = Math.min(0.033, (now - last) / 1000);
    last = now;
    update(dt);
    draw();
    requestAnimationFrame(loop);
  }
  requestAnimationFrame(loop);
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
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
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
    days = float(data.get("expiry_days") or 24)
    pw   = data.get("password") or ""
    title_raw = (data.get("title") or "").strip()
    title = title_raw[:120] if title_raw else None
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
    token = d.get("token"); filename = secure_filename(d.get("filename") or "")
    content_type = d.get("contentType") or "application/octet-stream"
    if not token or not filename:
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
    token = d.get("token"); key = d.get("key"); name = d.get("name")
    path  = d.get("path") or name
    if not (token and key and name):
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
    token = data.get("token")
    filename = secure_filename(data.get("filename") or "")
    content_type = data.get("contentType") or "application/octet-stream"
    if not token or not filename:
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
    token     = data.get("token"); key = data.get("key")
    name      = data.get("name");  path = data.get("path") or name
    parts_in  = data.get("parts") or []; upload_id = data.get("uploadId")
    client_size = int(data.get("clientSize") or 0)
    if not (token and key and name and parts_in and upload_id):
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
                c.execute("""INSERT OR REPLACE INTO subscriptions(login_email, plan_value, subscription_id, status, created_at)
                             VALUES(?,?,?,?,?)""",
                          (AUTH_EMAIL, plan_value or (plan_id or ""), sub_id, status, datetime.now(timezone.utc).isoformat()))
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

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
