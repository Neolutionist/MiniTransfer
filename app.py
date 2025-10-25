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

AUTH_EMAIL = os.environ.get("AUTH_EMAIL", "info@oldehanter.nl")
AUTH_PASSWORD = "Hulsmaat"  # vast wachtwoord voor het inloggen

S3_BUCKET       = os.environ["S3_BUCKET"]
S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]

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
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Bestanden delen met Olde Hanter</title>{{ head_icon|safe }}
<style>
{{ base_css }}
:root{ --ok:#16a34a; --warn:#eab308; --err:#dc2626; }

/* Titel/teksten met hoog contrast op donkere kaart */
h1{margin:.25rem 0 1rem;color:var(--brand);font-size:2.1rem}
.card{color:var(--text)}
label{color:var(--text)}
.smallmuted{color:var(--muted)}

.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
.logout a{color:var(--brand);text-decoration:none;font-weight:700}
.grid{display:grid;gap:1rem}
.cols-2{grid-template-columns:1fr 1fr}
@media (max-width:760px){.cols-2{grid-template-columns:1fr}}

/* Toggle */
.toggle{display:flex;gap:.9rem;align-items:center;margin:.25rem 0 .6rem}
.toggle label{display:inline-flex;gap:.5rem;align-items:center;font-weight:600;cursor:pointer;color:var(--text)}
.toggle input{accent-color:var(--brand)}

/* File list cards */
.filelist{margin-top:.8rem}
.filecard{
  display:grid;grid-template-columns:1fr auto;gap:.4rem .8rem;
  padding:.75rem 1rem;border:1px solid var(--line);border-radius:12px;
  background:color-mix(in oklab, var(--surface) 86%, white 14%); color:var(--text)
}
.filecard .name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.filecard .meta{color:var(--muted);font-size:.9rem}
.filecard .act{display:flex;gap:.4rem}
.badge{display:inline-block;padding:.22rem .55rem;border-radius:999px;font-size:.78rem;font-weight:700}
.badge.ok{background:color-mix(in oklab, var(--ok) 16%, white 84%);color:var(--ok)}
.badge.err{background:color-mix(in oklab, var(--err) 16%, white 84%);color:var(--err)}
.badge.warn{background:color-mix(in oklab, var(--warn) 16%, white 84%);color:var(--warn)}
.row{display:flex;align-items:center;gap:.6rem}

/* Progress */
.progress{height:12px;margin:.25rem 0 0;border-radius:999px;background:#eef2ff;overflow:hidden;border:1px solid #dbe5f4}
.progress > i{display:block;height:100%;width:0%;background:linear-gradient(90deg,#0f4c98,#1e90ff);transition:width .12s ease}

/* Total */
.totalbox{margin-top:1rem}
.kv{display:grid;grid-template-columns:auto 1fr;gap:.25rem .75rem;font-size:.92rem}
.kv strong{font-weight:700;color:var(--text)}
.kv span{color:var(--muted)}

/* Buttons */
.btn.icon{display:inline-flex;align-items:center;gap:.4rem}

/* zachter glas-effect achter login chip */
.topbar .logout{
  background: color-mix(in oklab, var(--surface) 75%, rgba(0,0,0,0) 25%);
  border: 1px solid color-mix(in oklab, var(--line) 80%, transparent 20%);
  box-shadow: 0 2px 16px rgba(0,0,0,.12);
}
@media(prefers-color-scheme: dark){
  .topbar .logout{
    background: color-mix(in oklab, var(--surface) 65%, transparent 35%);
    border-color: rgba(255,255,255,.12);
    box-shadow: 0 2px 16px rgba(0,0,0,.42);
  }
}

/* ================== File input perfect uitlijnen ================== */
:root{ --field-h: 48px; } /* één uniforme veldhoogte */

input.input,
select.input,
.input,
input[type=text],
input[type=password],
input[type=email],
input[type=number],
textarea{
  height: var(--field-h);
  padding: 0 1rem;               /* horizontale padding, verticaal via height */
  line-height: 1.2;
}

/* Het echte file-veld */
input[type=file]{
  height: var(--field-h);
  padding: 0 1rem;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: color-mix(in oklab, var(--surface-2) 90%, white 10%);
  color: var(--text);
  vertical-align: middle;
  position: relative;
  top: -1px;                     /* micro-nudge voor perfecte uitlijning */
}

/* De “Choose files” knop binnen het file-veld */
input[type=file]::file-selector-button{
  font: inherit;
  line-height: 1;
  padding: .50rem .90rem;
  height: calc(var(--field-h) - 18px); /* centreert de knop in het veld */
  border-radius: 10px;
  border: 1px solid var(--line);
  background: var(--surface);
  color: var(--text);
  margin-right: .75rem;
  cursor: pointer;
}

/* Dark mode varianten */
@media (prefers-color-scheme: dark){
  input[type=file]{
    background: color-mix(in oklab, var(--surface-2) 92%, black 8%);
    border-color: #374151;
  }
  input[type=file]::file-selector-button{
    background: var(--surface);
    border-color: #374151;
  }
}

</style>
</head><body>
{{ bg|safe }}

<div class="wrap">
  <div class="topbar">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div class="logout">Ingelogd als {{ user }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  <div class="card">
    <form id="form" class="grid cols-2" autocomplete="off" enctype="multipart/form-data">
      <div>
        <label>Uploadtype</label>
        <div class="toggle">
          <label id="lblFiles"><input id="modeFiles" type="radio" name="upmode" value="files" checked> Bestand(en)</label>
          <label id="lblFolder"><input id="modeFolder" type="radio" name="upmode" value="folder"> Map</label>
        </div>

        <div id="fileRow">
          <label for="fileInput">Kies bestand(en)</label>
          <input id="fileInput" class="input" type="file" multiple style="max-width:100%">
        </div>

        <div id="folderRow" style="display:none">
          <label for="folderInput">Kies een map</label>
          <input id="folderInput" class="input" type="file" multiple webkitdirectory directory style="max-width:100%">
        </div>

        <div class="smallmuted">Tip: gebruik de map-stand voor het uploaden van complete mappen met submappen.</div>
      </div>

      <div class="grid">
        <div>
          <label for="title">Onderwerp (optioneel)</label>
          <input id="title" class="input" type="text" placeholder="Bijv. Tekeningen project X" maxlength="120">
        </div>
        <div class="grid cols-2">
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
            <input id="pw" class="input" type="password" placeholder="Optioneel" autocomplete="new-password" autocapitalize="off" spellcheck="false">
          </div>
        </div>

        <!-- Alleen nog de Uploaden-knop (Pauzeer/Annuleer verwijderd) -->
        <div class="row" style="gap:.6rem;flex-wrap:wrap">
          <button id="btnStart" class="btn icon" type="submit"><span>Uploaden</span></button>
        </div>
      </div>
    </form>

    <div class="totalbox">
      <div class="row" style="justify-content:space-between">
        <strong style="color:var(--text)">Totaalvoortgang</strong>
        <span id="totalPct" class="badge warn">0%</span>
      </div>
      <div class="progress" id="totalBar"><i></i></div>
      <div class="kv" style="margin-top:.35rem">
        <strong>Status</strong><span id="totalStatus">Nog niet gestart</span>
        <strong>Snelheid</strong><span id="totalSpeed">–</span>
        <strong>ETA</strong><span id="totalEta">–</span>
      </div>
    </div>

    <div class="filelist" id="fileList" style="display:none"></div>
    <div id="result" style="margin-top:1rem"></div>
  </div>

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
// ===== helpers =====
function fmtBytes(n){const u=["B","KB","MB","GB","TB"];let i=0,x=n||0;while(x>=1024&&i<u.length-1){x/=1024;i++;}return(i?x.toFixed(1):Math.round(x))+" "+u[i];}
function sanitizePath(p){const parts=(p||"").split('/').map(n=>{const d=n.lastIndexOf('.');const b=d>=0?n.slice(0,d):n;const e=d>=0?n.slice(d):'';let s=b.normalize('NFKD').replace(/[\u0300-\u036f]/g,'').replace(/[^\w.\-]+/g,'_').replace(/_+/g,'_').replace(/^_+|_+$/g,'');if(s.length>160)s=s.slice(0,160);return(s||'file')+e.replace(/[^.\w-]/g,'');});return parts.join('/');}
function sleep(ms){ return new Promise(r=>setTimeout(r,ms)); }

// ===== DOM =====
const modeFiles = document.getElementById('modeFiles');
const modeFolder = document.getElementById('modeFolder');
const lblFolder = document.getElementById('lblFolder');
const fileRow = document.getElementById('fileRow');
const folderRow = document.getElementById('folderRow');
const fileInput = document.getElementById('fileInput');
const folderInput = document.getElementById('folderInput');

const form = document.getElementById('form');
const totalBar = document.getElementById('totalBar').querySelector('i');
const totalPct = document.getElementById('totalPct');
const totalStatus = document.getElementById('totalStatus');
const totalSpeed = document.getElementById('totalSpeed');
const totalEta = document.getElementById('totalEta');
const fileList = document.getElementById('fileList');
const resBox = document.getElementById('result');
const btnStart = document.getElementById('btnStart');

// iOS: directory input werkt niet -> verberg map-optie
(function(){
  const ua = navigator.userAgent||navigator.vendor||window.opera;
  const isIOS = /iPad|iPhone|iPod/.test(ua) || (navigator.platform==='MacIntel' && navigator.maxTouchPoints>1);
  if(isIOS){ modeFolder.disabled=true; lblFolder.style.display='none'; modeFiles.checked=true; }
})();
function applyMode(openPicker){
  const mode = document.querySelector('input[name="upmode"]:checked').value;
  fileRow.style.display = (mode==='files') ? '' : 'none';
  folderRow.style.display = (mode==='folder') ? '' : 'none';
  if(openPicker===true){
    try{ (mode==='files' ? fileInput : folderInput).click(); }catch(e){}
  }
}
document.querySelectorAll('input[name="upmode"]').forEach(r=>r.addEventListener('change',()=>applyMode(true)));
applyMode(false);

// ===== State =====
let state = { files:[], token:null, totalBytes:1, uploadedBytes:0, startedAt:0, paused:false };

// ===== UI helpers =====
function setTotal(pct,label){const p=Math.max(0,Math.min(100,pct||0)); totalBar.style.width=p+'%'; totalPct.textContent=Math.round(p)+'%'; totalPct.className='badge '+(p>=100?'ok':(p>0?'warn':'')); if(label) totalStatus.textContent=label; }
function setSpeedAndEta(uploaded,total,startedAt){const dt=(performance.now()-startedAt)/1000; const speed=dt>0?uploaded/dt:0; const remain=Math.max(0,total-uploaded); const eta=speed>0?remain/speed:0; totalSpeed.textContent=speed?fmtBytes(speed)+'/s':'–'; totalEta.textContent=speed?(eta>3600?(eta/3600).toFixed(1)+' u':(eta/60).toFixed(1)+' min'):'–';}
function addFileRow(file, path){
  fileList.style.display='';
  const card = document.createElement('div');
  card.className = 'filecard';
  card.innerHTML = `
    <div class="name" title="${path}">${path}</div>
    <div class="act"><span class="badge warn" data-role="badge">Wacht…</span></div>
    <div class="meta">${fmtBytes(file.size)} • ${file.type || 'octet-stream'}</div><div></div>
    <div class="progress"><i style="width:0%"></i></div>
    <div class="smallmuted" data-role="label">Nog niet gestart</div>
  `;
  fileList.appendChild(card);
  return {
    el: card,
    fill: card.querySelector('.progress i'),
    badge: card.querySelector('[data-role=badge]'),
    label: card.querySelector('[data-role=label]')
  };
}
function resetUploadUI(){
  fileList.innerHTML = ''; fileList.style.display = 'none';
  resBox.innerHTML = '';
  setTotal(0, 'Nog niet gestart'); totalSpeed.textContent = '–'; totalEta.textContent = '–';
  state = { files:[], token:null, totalBytes:1, uploadedBytes:0, startedAt:0, paused:false };
}
function selectedMode(){ return document.querySelector('input[name="upmode"]:checked').value; }
function currentFiles(){ const m=selectedMode(); return Array.from(m==='files'?(fileInput.files||[]):(folderInput.files||[])); }
function relP(file){ return selectedMode()==='folder' ? (file.webkitRelativePath || file.name) : file.name; }

// ===== server calls =====
async function packageInit(expiryDays,password,title){
  const r = await fetch("{{ url_for('package_init') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expiry_days:expiryDays,password:password||"",title:title||""})});
  const j = await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'package-init mislukt'); return j.token;
}
async function putInit(token,filename,type){
  const r=await fetch("{{ url_for('put_init') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,filename,contentType:type||"application/octet-stream"})});
  const j=await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'put-init mislukt'); return j;
}
async function putComplete(token,key,name,path){
  const r=await fetch("{{ url_for('put_complete') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,key,name,path})});
  const j=await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'afronden mislukt'); return j;
}
async function mpuInit(token,filename,type){
  const r=await fetch("{{ url_for('mpu_init') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,filename,contentType:type||"application/octet-stream"})});
  const j=await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'mpu-init mislukt'); return j;
}
async function mpuSign(key,uploadId,partNumber){
  const r=await fetch("{{ url_for('mpu_sign') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({key,uploadId,partNumber})});
  const j=await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'part-sign mislukt'); return j.url;
}
async function mpuComplete(token,key,name,path,parts,uploadId,clientSize){
  const r=await fetch("{{ url_for('mpu_complete') }}",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({token,key,name,path,parts,uploadId,clientSize})});
  const j=await r.json(); if(!r.ok||!j.ok) throw new Error(j.error||'mpu-complete mislukt'); return j;
}
function putWithProgress(url,blob,onProgress,onAbortSetter){
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();
    xhr.open("PUT",url,true);
    xhr.timeout=30*60*1000;
    xhr.setRequestHeader("Content-Type",blob.type||"application/octet-stream");
    xhr.upload.onprogress=(ev)=>{const loaded=ev.loaded||0; const total=ev.total||blob.size||1; onProgress(loaded,total);};
    xhr.onload=()=> (xhr.status>=200&&xhr.status<300)?resolve(xhr.getResponseHeader("ETag")?.replaceAll('"','')||null):reject(new Error(`HTTP ${xhr.status} ${xhr.statusText}`));
    xhr.onerror=()=>reject(new Error("Netwerkfout bij upload"));
    xhr.ontimeout=()=>reject(new Error("Upload timeout"));
    xhr.send(blob);
    if(typeof onAbortSetter==='function'){ onAbortSetter(()=>xhr.abort()); }
  });
}

// ===== uploader =====
async function uploadOneSmall(token,file,rel,ui){
  const init=await putInit(token,file.name,file.type);
  ui.badge.textContent="Uploaden…"; ui.label.textContent="Directe upload";
  await putWithProgress(init.url,file,(l,t)=>{ui.fill.style.width=Math.round(l/t*100)+'%';},fn=>ui.abort=fn);
  await putComplete(token,init.key,file.name,rel);
  ui.fill.style.width='100%'; ui.badge.textContent="Klaar"; ui.badge.className='badge ok'; ui.label.textContent="Opgeslagen";
}
async function uploadOneMPU(token,file,rel,ui){
  const CHUNK=24*1024*1024, PAR=6;
  const init=await mpuInit(token,file.name,file.type);
  const key=init.key, uploadId=init.uploadId;
  const parts=Math.ceil(Math.max(1,file.size)/CHUNK); const doneBytes=new Array(parts).fill(0);
  let abortFns=[];
  ui.badge.textContent="Uploaden…"; ui.label.textContent="Multipart ("+parts+" delen)";
  function refresh(){const uploaded=doneBytes.reduce((a,b)=>a+b,0); ui.fill.style.width=Math.round(uploaded/Math.max(1,file.size)*100)+'%';}
  async function sendPart(n){
    const i=n-1, start=i*CHUNK, end=Math.min(start+CHUNK,file.size); const blob=file.slice(start,end);
    const url=await mpuSign(key,uploadId,n); let abort=()=>{};
    const etag=await putWithProgress(url,blob,(l)=>{doneBytes[i]=Math.min(l,blob.size);refresh();},fn=>abort=fn);
    abortFns[i]=abort; doneBytes[i]=blob.size; refresh(); return {PartNumber:n, ETag:etag};
  }
  ui.abort=()=>{abortFns.forEach(fn=>{try{fn&&fn();}catch(_){}}); throw new Error("geannuleerd");};
  const results=new Array(parts); let next=1;
  async function worker(){while(true){ if(state.paused){await sleep(150); continue;} const m=next++; if(m>parts) break; results[m-1]=await sendPart(m);} }
  await Promise.all(Array.from({length:Math.min(PAR,parts)},worker));
  await mpuComplete(token,key,file.name,rel,results,uploadId,file.size);
  ui.fill.style.width='100%'; ui.badge.textContent="Klaar"; ui.badge.className='badge ok'; ui.label.textContent="Opgeslagen";
}

async function runUpload(){
  resetUploadUI();
  const filesRaw=currentFiles();
  if(!filesRaw.length){ alert("Kies eerst bestand(en) of map"); try{ (selectedMode()==='files'?fileInput:folderInput).click(); }catch(e){} return; }

  state.files = filesRaw.map(f=>{ const rp=sanitizePath(relP(f)); const ui=addFileRow(f,rp); return {file:f, rel:rp, ui}; });
  state.totalBytes = state.files.reduce((a,x)=>a+x.file.size,0)||1; state.uploadedBytes=0; state.startedAt=performance.now();
  setTotal(0,"Voorbereiden…"); totalSpeed.textContent='–'; totalEta.textContent='–'; resBox.innerHTML="";

  const expiryDays=document.getElementById('expDays').value;
  const password=document.getElementById('pw').value||'';
  const title=document.getElementById('title').value||'';
  state.token = await packageInit(expiryDays,password,title);

  for(const item of state.files){
    if(state.paused) while(state.paused){ await sleep(150); }
    const startUploaded=state.uploadedBytes;

    const updateTotal=()=>{
      const per=parseFloat(item.ui.fill.style.width)||0;
      const added=Math.round(item.file.size*per/100);
      const cur=startUploaded+added;
      setTotal((cur/state.totalBytes)*100,"Uploaden…");
      const uploadedSum = state.uploadedBytes + added;
      setSpeedAndEta(uploadedSum, state.totalBytes, state.startedAt);
    };
    Object.defineProperty(item.ui.fill.style,'width',{set(v){this.setProperty('width',v);requestAnimationFrame(updateTotal);},get(){return this.getPropertyValue('width');}});

    try{
      if(item.file.size < 5*1024*1024) await uploadOneSmall(state.token,item.file,item.rel,item.ui);
      else await uploadOneMPU(state.token,item.file,item.rel,item.ui);
      state.uploadedBytes += item.file.size;
      setTotal((state.uploadedBytes/state.totalBytes)*100,"Uploaden…");
      setSpeedAndEta(state.uploadedBytes, state.totalBytes, state.startedAt);
    }catch(err){
      if(String(err||'').includes("geannuleerd")){
        item.ui.badge.textContent="Geannuleerd"; item.ui.badge.className='badge err'; item.ui.label.textContent="Onderbroken"; throw err;
      }else{
        item.ui.badge.textContent="Fout"; item.ui.badge.className='badge err'; item.ui.label.textContent=(err&&err.message)?err.message:"Onbekende fout";
      }
    }
  }

  setTotal(100,"Klaar"); totalSpeed.textContent='–'; totalEta.textContent='–';
  const link="{{ url_for('package_page', token='__T__', _external=True) }}".replace("__T__", state.token);
  resBox.innerHTML=`<div class="card" style="margin-top:1rem"><strong>Deelbare link</strong><div class="row" style="gap:.5rem;margin-top:.35rem"><input id="shareLinkInput" class="input" style="flex:1" value="${link}" readonly><button class="btn" type="button" id="copyBtn">Kopieer</button><span id="copyOk" class="small" style="display:none;margin-left:.25rem;">Gekopieerd!</span></div></div>`;
  const copyBtn=document.getElementById('copyBtn'); const copyOk=document.getElementById('copyOk'); const input=document.getElementById('shareLinkInput');
  copyBtn.addEventListener('click',async()=>{try{await (navigator.clipboard?.writeText(input.value));}catch(_){input.select();document.execCommand?.('copy');}copyOk.style.display='inline';setTimeout(()=>copyOk.style.display='none',1600);});
}

// ===== events =====
function maybeResetOnNewSelection(){ resetUploadUI(); }
fileInput.addEventListener('change', maybeResetOnNewSelection);
folderInput.addEventListener('change', maybeResetOnNewSelection);
document.querySelectorAll('input[name="upmode"]').forEach(r=> r.addEventListener('change', maybeResetOnNewSelection));

// Submit
form.addEventListener('submit', async (e)=>{
  e.preventDefault();
  btnStart.disabled = true;
  try { await runUpload(); }
  catch(_) { totalStatus.textContent = 'Geannuleerd'; }
  finally { btnStart.disabled = false; }
});
</script>
</body></html>
"""


PACKAGE_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Download – Olde Hanter</title>{{ head_icon|safe }}
<style>
{{ base_css }}
h1{margin:.2rem 0 1rem;color:var(--brand)}
/* Meta-informatie: gebruik thema-kleuren zodat het in light én dark goed leesbaar is */
.meta{margin:.6rem 0 1rem;color:var(--muted)}
.meta strong{color:var(--text)}
.btn{padding:.85rem 1.15rem;border-radius:12px;background:var(--brand);color:#fff;text-decoration:none;font-weight:700}
.btn.secondary{background:#0f4c98}
.btn.mini{padding:.5rem .75rem;font-size:.9rem;border-radius:10px}

/* Tabel-kolom 'Grootte' blijft op één regel (ook mobiel) */
.table th.col-size,
.table td.col-size,
.table td[data-label="Grootte"]{
  white-space: nowrap;
  text-align: right;
  min-width: 72px;
}

/* Zorg dat CTA nooit onder de kaart schuift */
.cta{margin-top:1.2rem; position:relative; z-index:2}


/* ============================================
   Compacte downloadknoppen in de tabel
   ============================================ */
.table .btn.mini {
  display:inline-flex;
  align-items:center;
  justify-content:center;
  padding:.42rem .72rem !important;
  font-size:.82rem !important;
  line-height:1 !important;
  font-weight:600 !important;
  border-radius:10px !important;
  white-space:nowrap;

  /* Nieuwe stijl (licht + zacht) */
  background: color-mix(in oklab, var(--brand) 70%, white 30%);
  color:#fff;
  border:0;
  box-shadow:0 2px 6px rgba(0,0,0,.12);

  transition:filter .15s, transform .12s, background .2s;
}

.table .btn.mini:hover {
  filter:brightness(1.06);
}
.table .btn.mini:active {
  transform:translateY(1px);
}

/* Geschikt voor dark mode */
@media (prefers-color-scheme: dark){
  .table .btn.mini {
    background: color-mix(in oklab, var(--brand) 85%, black 15%);
    box-shadow:0 2px 7px rgba(0,0,0,.4);
  }
}

/* tabeluitlijnen subtiel verbeteren */
.table td[data-label="Grootte"]{
  padding-right:.4rem;
}

/* ===== POLISH: tabel, knoppen, kleuren, spacing ===== */

/* Rustiger, modern CTA (alleen op deze pagina) */
.btn{
  padding:.85rem 1.05rem;
  border:0;
  border-radius:12px;
  font-weight:700;
  color:#fff;
  text-decoration:none;
  background:linear-gradient(
    180deg,
    color-mix(in oklab, var(--brand) 88%, white 12%) 0%,
    color-mix(in oklab, var(--brand) 80%, black 20%) 100%
  );
  box-shadow:0 8px 20px rgba(15,76,152,.22);
  transition:filter .15s, transform .12s, background .2s;
}
.btn:hover{ filter:brightness(1.06); }
.btn:active{ transform:translateY(1px); }

/* Secundaire CTA (onder de kaart) */
.btn.secondary{
  background:linear-gradient(
    180deg,
    color-mix(in oklab, var(--brand) 70%, black 10%) 0%,
    color-mix(in oklab, var(--brand) 60%, black 28%) 100%
  );
  box-shadow:0 6px 16px rgba(15,76,152,.20);
}

/* Compacte mini-downloadknoppen in de tabel */
.table .btn.mini{
  display:inline-flex; align-items:center; justify-content:center;
  padding:.42rem .72rem !important;
  font-size:.82rem !important; line-height:1 !important; font-weight:600 !important;
  border-radius:10px !important; white-space:nowrap;
  background: color-mix(in oklab, var(--brand) 74%, white 26%);
  color:#fff; border:0; box-shadow:0 2px 6px rgba(0,0,0,.12);
  transition:filter .15s, transform .12s, background .2s;
}
.table .btn.mini:hover{ filter:brightness(1.06); }
.table .btn.mini:active{ transform:translateY(1px); }

/* Tabelpolish: iets meer ademruimte en subtiele lijnen */
.table{ width:100%; border-collapse:separate; border-spacing:0 6px; }
.table thead th{
  font-weight:700; color:var(--text); opacity:.9; padding:.4rem .7rem;
}
.table tbody tr{
  background: color-mix(in oklab, var(--surface-2) 80%, white 20%);
  box-shadow: 0 1px 0 rgba(0,0,0,.05) inset, 0 0 0 1px rgba(0,0,0,.05);
  border-radius:12px;
}
.table tbody td{
  padding:.6rem .7rem;
  border-bottom:0;
}
.table tbody tr:hover{
  background: color-mix(in oklab, var(--surface-2) 70%, white 30%);
}

/* Kolom 'Grootte' netjes rechts + smal */
.table th.col-size,
.table td.col-size,
.table td[data-label="Grootte"]{ white-space:nowrap; text-align:right; min-width:72px; padding-right:.4rem; }

/* Pad-kolom: ellipsis en vaste max-breedte — JS verkort straks tot laatste 2 mappen */
.table td[data-label="Pad"]{
  max-width: 520px;
  overflow:hidden;
  text-overflow:ellipsis;
  white-space:nowrap;
}

/* Progressbalk onder de ZIP-knop: kleine marge */
.progress#bar{ margin-top:.75rem; }

/* Dark mode-varianten */
@media (prefers-color-scheme: dark){
  .btn{
    background:linear-gradient(
      180deg,
      color-mix(in oklab, var(--brand) 92%, black 12%) 0%,
      color-mix(in oklab, var(--brand) 78%, black 28%) 100%
    );
    box-shadow:0 10px 24px rgba(0,0,0,.45);
  }
  .btn.secondary{
    background:linear-gradient(
      180deg,
      color-mix(in oklab, var(--brand) 80%, black 22%) 0%,
      color-mix(in oklab, var(--brand) 68%, black 40%) 100%
    );
    box-shadow:0 8px 20px rgba(0,0,0,.42);
  }
  .table tbody tr{
    background: color-mix(in oklab, var(--surface-2) 92%, black 8%);
    box-shadow: 0 1px 0 rgba(255,255,255,.04) inset, 0 0 0 1px rgba(255,255,255,.06);
  }
  .table tbody tr:hover{
    background: color-mix(in oklab, var(--surface-2) 86%, black 14%);
  }
  .table thead th{ opacity:.95; }
}

/* ==== Dynamische zwarte slang (downloadpagina) ====================== */
#snakeWrap{
  position:fixed;
  z-index:9999;
  width:150px;              /* totale grootte slang */
  height:100px;
  left:0; top:0;            /* we positioneren ALLES met translate(...) */
  transform:translate3d(24px, 72vh, 0);
  will-change:transform;
  cursor:pointer;
  user-select:none;
}
#snakeWrap svg{ width:100%; height:100%; overflow:visible; }

/* Tekstballon */
#snakeBubble{
  position:absolute;
  bottom:72px;
  left:-10px;
  width:190px;
  background:#fff;
  color:#111;
  border:1px solid rgba(0,0,0,.15);
  padding:.5rem .7rem;
  border-radius:10px;
  box-shadow:0 10px 24px rgba(0,0,0,.25);
  font-size:.85rem; line-height:1.25;
  opacity:0; transform:translateY(8px);
  pointer-events:none;
  transition:opacity .2s, transform .2s;
    max-width: min(220px, calc(100vw - 40px));
  will-change: transform, opacity;
}
#snakeBubble.show{ opacity:1; transform:translateY(0); }
#snakeBubble:after{
  content:"";
  position:absolute; left:26px; bottom:-10px;
  border-width:10px 8px 0 8px; border-style:solid;
  border-color:#fff transparent transparent transparent;
}


/* --- Mooie afgeronde rijen voor bestandslijst --- */
.table tbody tr {
  background: color-mix(in oklab, var(--surface-2) 88%, black 12%);
  border: 1px solid color-mix(in oklab, var(--line) 70%, black 10%);
  border-radius: 14px;
  overflow: hidden;
}

.table tbody td {
  padding: .75rem .9rem;
  border-bottom: none !important;
}

/* Ruimte tussen de rijen */
.table tbody tr + tr {
  margin-top: .55rem;
}

/* Scheiding header ↔ rijen */
.table {
  border-collapse: separate;
  border-spacing: 0 8px;
}

/* Download-knop beter in stijl */
.table .btn.mini {
  padding: .45rem .7rem;
  font-size: .85rem;
  border-radius: 10px;
  background: linear-gradient(180deg, var(--brand), color-mix(in oklab, var(--brand) 85%, black 15%));
  white-space: nowrap;
}

/* Subtiele hover animatie */
.table tbody tr:hover {
  background: color-mix(in oklab, var(--surface-2) 82%, black 18%);
  transform: translateY(-1px);
  transition: background .18s ease, transform .18s ease;
}

/* Zichtbaarheid/contrast slang */
#snakeWrap { 
  z-index: 2147483647; /* boven ALLES */
}

#snakeWrap svg { 
  filter: drop-shadow(0 0 6px rgba(255,255,255,.65))
          drop-shadow(0 2px 10px rgba(0,0,0,.35));
}

#snakeWrap #body { 
  stroke: #000; 
  stroke-width: 14; 
}

#snakeWrap #head > circle:first-child {
  fill: #000;
}


</style></head><body>
{{ bg|safe }}

<div class="wrap">
  <div class="card" style="position:relative; z-index:1">
    <h1>Download</h1>
    <div class="meta">
      <div><strong>Onderwerp:</strong> {{ title or token }}</div>
      <div><strong>Verloopt:</strong> {{ expires_human }}</div>
      <div><strong>Totaal:</strong> {{ total_human }}</div>
      <div><strong>Bestanden:</strong> {{ items|length }}</div>
    </div>

{% if items|length == 1 %}
  <a
    class="btn"
    id="dlSingle"
    href="{{ url_for('stream_file', token=token, item_id=items[0]['id']) }}"
    download="{{ items[0]['name'] }}"
    rel="noopener"
  >
    Download
  </a>
{% else %}
  <a
    class="btn"
    id="zipAll"
    href="{{ url_for('stream_zip', token=token) }}"
    download
    rel="noopener"
  >
    Alles downloaden (zip)
  </a>
{% endif %}

    <div class="progress" id="bar" style="display:none"><i></i></div>
    <div class="small" id="txt" style="display:none">Starten…</div>

    {% if items|length > 1 %}
    <table class="table">
      <thead>
        <tr>
          <th>Bestand</th>
          <th>Pad</th>
          <th class="col-size">Grootte</th>
          <th style="width:1%"></th>
        </tr>
      </thead>
      <tbody>
        {% for it in items %}
        <tr>
          <td data-label="Bestand">{{ it["name"] }}</td>
          <td class="small" data-label="Pad">{{ it["path"] }}</td>
          <td class="col-size" data-label="Grootte">{{ it["size_h"] }}</td>
          <td data-label=""><a class="btn mini" href="{{ url_for('stream_file', token=token, item_id=it['id']) }}">Download</a></td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    {% endif %}
  </div>

  <div class="cta">
    <a class="btn secondary" href="{{ url_for('contact') }}">Eigen transfer-oplossing aanvragen</a>
  </div>

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  const bar  = document.getElementById('bar');
  const fill = bar?.querySelector('i');
  const txt  = document.getElementById('txt');

  function nativeDownload(url, suggestedName){
    try{
      const a = document.createElement('a');
      a.href = url;
      if (suggestedName) a.download = suggestedName;
      a.rel = 'noopener';
      a.target = '_self';
      document.body.appendChild(a);
      a.click();
      a.remove();
    }catch(_){
      window.location.href = url;
    }
  }

  async function streamToBlob(url, fallbackName){
    try{
      if (bar && txt && fill){
        bar.style.display='block';
        txt.style.display='block';
        fill.style.width='0%';
        txt.textContent='Starten…';
      }
      const res = await fetch(url, { credentials: 'same-origin' });
      if(!res.ok){
        const xerr = res.headers.get('X-Error') || '';
        let body=''; try{ body = await res.text(); }catch(e){}
        alert(`Fout ${res.status}${xerr ? ' – ' + xerr : ''}${body ? '\n\n' + body : ''}`);
        if (bar && txt){ bar.style.display='none'; txt.style.display='none'; }
        return;
      }
      const total = parseInt(res.headers.get('Content-Length')||'0',10);
      const name  = res.headers.get('X-Filename') || fallbackName || 'download';

      if (bar){ total ? bar.classList.remove('indet') : bar.classList.add('indet'); }

      const reader = res.body && res.body.getReader ? res.body.getReader() : null;
      if (reader){
        const chunks=[]; let received=0;
        while(true){
          const {done,value} = await reader.read();
          if(done) break;
          chunks.push(value); received += value.length;
          if (fill && txt){
            if (total){
              const p = Math.round(received/total*100);
              fill.style.width = p+'%'; txt.textContent = p+'%';
            }else{
              txt.textContent = (received/1024/1024).toFixed(1)+' MB…';
            }
          }
        }
        if (bar){ bar.classList.remove('indet'); }
        if (fill){ fill.style.width='100%'; }
        if (txt){ txt.textContent='Klaar'; }

        const blob = new Blob(chunks);
        const u = URL.createObjectURL(blob);
        nativeDownload(u, name);
        URL.revokeObjectURL(u);
        if (bar && txt) setTimeout(()=>{ bar.style.display='none'; txt.style.display='none'; }, 800);
        return;
      }

      const blob = await res.blob();
      const u = URL.createObjectURL(blob);
      nativeDownload(u, name);
      URL.revokeObjectURL(u);
      if (bar && txt){ bar.style.display='none'; txt.style.display='none'; }
    }catch(err){
      console.error('Stream fallback naar native:', err);
      nativeDownload(url, null);
      if (bar && txt){ bar.style.display='none'; txt.style.display='none'; }
    }
  }

  // Alleen voor het geval er maar één bestand is: progress aan
{% if items|length == 1 %}
  document.getElementById('dlSingle')?.addEventListener('click', (e)=>{
    e.preventDefault();
    streamToBlob("{{ url_for('stream_file', token=token, item_id=items[0]['id']) }}",
                 "{{ items[0]['name'] }}");
  });
{% endif %}

/* ===== POLISH: Pad-kolom inkorten tot laatste 2 segmenten + title op volledig pad ===== */
(function(){
  const cells = document.querySelectorAll('td[data-label="Pad"]');
  cells.forEach(td => {
    const full = (td.textContent || '').trim();
    if (!full) return;
    td.setAttribute('title', full);                // volledige pad als tooltip
    const parts = full.split('/').filter(Boolean);
    if (parts.length <= 2) return;
    td.textContent = parts.slice(-2).join('/');    // toon alleen laatste 2 segmenten
  });
})();

<!-- ====== Slang: klein, zwart, dynamisch ====== -->
<div id="snakeWrap" aria-label="speels slangetje">
  <svg viewBox="-20 -25 200 120" xmlns="http://www.w3.org/2000/svg">
    <g id="snakeGroup">
      <path id="spine" d="" fill="none" stroke="none"/>
      <path id="body"  d="" fill="none" stroke="#000" stroke-linecap="round" stroke-linejoin="round" stroke-width="14"/>
      <g id="head">
        <circle cx="0" cy="0" r="12" fill="#000"/>
        <circle cx="4" cy="-4" r="2.6" fill="#fff"/>
        <path d="M12 4 l10 1 -10 3" stroke="#000" stroke-width="2" fill="none" stroke-linecap="round"/>
      </g>
    </g>
  </svg>
  <div id="snakeBubble">…</div>
</div>

<script>
(function(){
  /* ---------- Jiskefet one-liners ---------- */
  const QUOTES = [
    "Ga jij nou es weg joh… ik ben ff bezig.",
    "Kijk uit! Mijn vader werkt bij de Rijkspolitie!",
    "Ja doei! Ik ben een slang, geen helpdesk.",
    "Hee lekker hoor… maar niet aankomen!",
    "Wil jij een broodje kaas ofzo?",
    "Ik ben niet gek, ik ben een slang!",
    "Kom op joh… ik heb ook maar twee handen!",
    "Ben jij los ofzo?",
    "Moet dat nou steeds?",
    "Ja hallo zeg… ik heb weekend!"
  ];

  /* ---------- DOM ---------- */
  const wrap   = document.getElementById('snakeWrap');
  const group  = document.getElementById('snakeGroup');
  const body   = document.getElementById('body');
  const head   = document.getElementById('head');
  const bubble = document.getElementById('snakeBubble');

  /* ---------- Kronkel-parameters ---------- */
  const L = 120;                  // lengte van de slang in SVG-units
  const N = 22;                   // aantal sample-punten
  let amp   = 6;                  // amplitude
  let freq  = 0.13;               // frequentie
  let phase = 0;                  // lopende fase
  let speed = 140;                // px/s
  let lastT = performance.now();

  /* ---------- Positie (top/left wereld, alles via translate) ---------- */
  function clamp(v, a, b){ return Math.max(a, Math.min(b, v)); }
  function rand(a,b){ return a + Math.random()*(b-a); }

  function pickSafeTarget(){
    const margin = 18;
    const W = Math.max(0, window.innerWidth  - wrap.clientWidth  - margin);
    const H = Math.max(0, window.innerHeight - wrap.clientHeight - margin);
    return { x: rand(margin, W), y: rand(margin, H) };
  }

  let pos    = pickSafeTarget();
  let target = pickSafeTarget();

  function applyTransform(){
    wrap.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
  }
  applyTransform();

  function moveTowards(dt){
    const dx = target.x - pos.x;
    const dy = target.y - pos.y;
    const dist = Math.hypot(dx,dy);
    if (dist < 1) return;
    const step = Math.min(dist, speed * dt);
    pos.x += (dx/dist) * step;
    pos.y += (dy/dist) * step;

    // klem binnen viewport
    const m = 18;
    pos.x = clamp(pos.x, m, Math.max(m, window.innerWidth  - wrap.clientWidth  - m));
    pos.y = clamp(pos.y, m, Math.max(m, window.innerHeight - wrap.clientHeight - m));

    applyTransform();
  }

  /* ---------- Slang-geometrie ---------- */
  function computeSpine(){
    const pts = [];
    for(let i=0;i<N;i++){
      const x = (L/(N-1))*i;
      const y = amp * Math.sin(freq * x + phase);
      pts.push([x,y]);
    }
    return pts;
  }
  function catmullRom2bezier(points){
    if(points.length < 2) return "";
    const d = [];
    for (let i = 0; i < points.length - 1; i++) {
      const p0 = points[i-1] || points[i];
      const p1 = points[i];
      const p2 = points[i+1];
      const p3 = points[i+2] || p2;
      const cp1x = p1[0] + (p2[0]-p0[0]) / 6;
      const cp1y = p1[1] + (p2[1]-p0[1]) / 6;
      const cp2x = p2[0] - (p3[0]-p1[0]) / 6;
      const cp2y = p2[1] - (p3[1]-p1[1]) / 6;
      if(i===0) d.push(`M ${p1[0]} ${p1[1]}`);
      d.push(`C ${cp1x} ${cp1y}, ${cp2x} ${cp2y}, ${p2[0]} ${p2[1]}`);
    }
    return d.join(" ");
  }
  function orientHead(points){
    const pLast = points[points.length-1];
    const pPrev = points[points.length-2];
    const ang = Math.atan2(pLast[1]-pPrev[1], pLast[0]-pPrev[0]) * 180/Math.PI;
    head.setAttribute('transform', `translate(${pLast[0]} ${pLast[1]}) rotate(${ang})`);
  }
  function rotateGroup(angleRad){
    const deg = angleRad * 180/Math.PI;
    group.setAttribute('transform', `rotate(${deg})`);
  }

  /* ---------- Interactie ---------- */
  let clicks = 0;
  wrap.addEventListener('click', () => {
    clicks++;
    target = pickSafeTarget();
    amp  = rand(5, 8);
    freq = rand(0.11, 0.16);

    if (clicks >= 3){
      bubble.textContent = QUOTES[Math.floor(Math.random()*QUOTES.length)];
      bubble.classList.add('show');
      setTimeout(()=> bubble.classList.remove('show'), 2400);
      clicks = 0;
    }
  }, { passive:true });

  /* ---------- Animatielus ---------- */
  function tick(t){
    const dt = (t - lastT)/1000; lastT = t;
    phase += 6.5 * dt;

    // richting voor de hele slang (kop vooruit)
    const dx = target.x - pos.x, dy = target.y - pos.y;
    rotateGroup(Math.atan2(dy, dx));

    const spine = computeSpine();
    body.setAttribute('d', catmullRom2bezier(spine));
    orientHead(spine);

    moveTowards(dt);
    requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);

  // bij resize in beeld houden
  window.addEventListener('resize', () => {
    const m = 18;
    pos.x = clamp(pos.x, m, Math.max(m, window.innerWidth  - wrap.clientWidth  - m));
    pos.y = clamp(pos.y, m, Math.max(m, window.innerHeight - wrap.clientHeight - m));
    applyTransform();
    target = pickSafeTarget();
  }, { passive:true });
})();
</script>


</body></html>
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
    if not pkg: c.close(); abort(404)

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
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
