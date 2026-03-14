

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

    # ===== ONLINE LEADERBOARD =====
    c.execute("""
      CREATE TABLE IF NOT EXISTS leaderboard_scores (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        player_name TEXT NOT NULL,
        score INTEGER NOT NULL,
        wave INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        ip TEXT,
        user_agent TEXT
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_leaderboard_created_at ON leaderboard_scores(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_leaderboard_score_wave ON leaderboard_scores(score DESC, wave DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_leaderboard_ip_created ON leaderboard_scores(ip, created_at)")

    c.commit()
    c.close()

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
  <p>Speel ondertussen de vernieuwde arcade challenge met rijkere arena, professionelere effecten, sector-based arena layout, uitgebreidere wapensystemen en een online leaderboard.</p>

  <div id="nameRow">
    <input id="playerName" maxlength="18" placeholder="Jouw naam" value="Speler"/>
  </div>

  <p>Desktop: <b>WASD</b>, <b>klik</b>, <b>1/2/3</b> voor wapens en <b>4/5/6</b> voor skills. Mobiel: <b>linker joystick beweegt</b>, <b>rechter joystick kijkt</b>, <b>tik om te schieten</b> en gebruik de <b>skillknoppen rechts</b>.</p>

  <button id="startBtn">Start spel</button>
  <div><button id="restartBtn" style="display:none;">Opnieuw spelen</button></div>

  <div id="boardWrap">
    <div class="board-meta">
      <h3>Leaderboard</h3>
      <span>Online leaderboard</span>
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


function escapeHtml(s){
  return String(s).replace(/[&<>"']/g, m => ({
    "&":"&amp;","<":"&lt;",">":"&gt;",
    '"':"&quot;","'":"&#39;"
  })[m]);
}

function getPlayerName(){
  return (ui.playerName.value || "Speler").trim().slice(0,18) || "Speler";
}

async function renderBoard(){
  ui.leaderboard.innerHTML = "<li>Leaderboard laden...</li>";

  try{
    const res = await fetch("/api/leaderboard/top?limit=10");
    if(!res.ok) throw new Error("HTTP error");

    const data = await res.json();
    const rows = data.rows || [];

    ui.leaderboard.innerHTML = rows.length
      ? rows.map(r =>
        `<li><b>${escapeHtml(r.name)}</b> — ${r.score} punten — wave ${r.wave}</li>`
      ).join("")
      : "<li>Nog geen scores</li>";

  }catch(err){
    console.error(err);
    ui.leaderboard.innerHTML = "<li>Leaderboard niet beschikbaar</li>";
  }
}

async function submitScore(){

  const score = Math.floor(player.score);

  if(score <= 0) return;

  await fetch("/api/leaderboard/submit", {

    method: "POST",

    headers: {
      "Content-Type": "application/json"
    },

    body: JSON.stringify({
      name: getPlayerName(),
      score: score,
      wave: player.wave || 0
    })

  });

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

function intersectsAnyCollider(box, padding = 0.02){
  const test = box.clone().expandByScalar(-padding);

  for(const c of colliders){
    if(test.intersectsBox(c.box)) return true;
  }
  return false;
}

function addBox(w, h, d, x, y, z, color = 0x243d84, opts = {}){
  const {
    preventOverlap = true,
    overlapPadding = 0.02,
    yEpsilon = 0.01
  } = opts;

  const geo = new THREE.BoxGeometry(w, h, d);
  const mat = new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: 0.12,
    roughness: 0.75,
    metalness: 0.15
  });

  const mesh = new THREE.Mesh(geo, mat);

  // klein hoogteverschil tegen coplanar z-fighting
  mesh.position.set(x, y + yEpsilon, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(mesh);

  if(preventOverlap && intersectsAnyCollider(box, overlapPadding)){
    console.warn("Blok niet geplaatst wegens overlap:", { w, h, d, x, y, z });
    geo.dispose();
    mat.dispose();
    return null;
  }

  scene.add(mesh);
  colliders.push({ mesh, box });
  return mesh;
}

function addCylinderCollider(radius, height, x, y, z, color = 0x1d2c4f, opts = {}){
  const {
    preventOverlap = true,
    overlapPadding = 0.02,
    yEpsilon = 0.01
  } = opts;

  const geo = new THREE.CylinderGeometry(radius, radius, height, 12);
  const mat = new THREE.MeshStandardMaterial({
    color,
    emissive: 0x0d1a33,
    emissiveIntensity: 0.35,
    roughness: 0.75,
    metalness: 0.18
  });

  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.set(x, y + yEpsilon, z);
  mesh.castShadow = true;
  mesh.receiveShadow = true;
  mesh.updateMatrixWorld(true);

  const box = new THREE.Box3().setFromObject(mesh);

  if(preventOverlap && intersectsAnyCollider(box, overlapPadding)){
    console.warn("Cylinder niet geplaatst wegens overlap:", { radius, height, x, y, z });
    geo.dispose();
    mat.dispose();
    return null;
  }

  scene.add(mesh);
  colliders.push({ mesh, box });
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
  ctx.clearRect(0,0,w,h);

  ctx.fillStyle = "rgba(5,10,20,.92)";
  ctx.fillRect(0,0,w,h);

  ctx.strokeStyle = "rgba(0,247,255,.18)";
  ctx.lineWidth = 1;
  for(let i=1;i<4;i++){
    ctx.beginPath();
    ctx.arc(w/2,h/2,(w*0.16)*i,0,Math.PI*2);
    ctx.stroke();
  }
  ctx.beginPath();
  ctx.moveTo(w/2,0); ctx.lineTo(w/2,h);
  ctx.moveTo(0,h/2); ctx.lineTo(w,h/2);
  ctx.stroke();

  const scale = 3.2;
  const ox = w/2 - player.pos.x*scale;
  const oy = h/2 - player.pos.z*scale;

  for(const obs of colliders){
    const b = obs.box;
    const x = ox + b.min.x*scale;
    const y = oy + b.min.z*scale;
    const ww = (b.max.x-b.min.x)*scale;
    const hh = (b.max.z-b.min.z)*scale;
    ctx.fillStyle = "rgba(140,170,220,.16)";
    ctx.fillRect(x,y,ww,hh);
  }

  for(const p of state.pickups){
    const x = ox + p.mesh.position.x*scale;
    const y = oy + p.mesh.position.z*scale;
    ctx.fillStyle =
      p.kind==="heal" ? "#39ff9c" :
      p.kind==="shield" ? "#7db7ff" :
      p.kind==="rocket" ? "#ff6b6b" :
      p.kind==="grenade" ? "#9cff57" :
      "#ffd24d";
    ctx.beginPath();
    ctx.arc(x,y,3,0,Math.PI*2);
    ctx.fill();
  }

  for(const hzd of state.hazards){
    const x = ox + hzd.mesh.position.x*scale;
    const y = oy + hzd.mesh.position.z*scale;
    ctx.fillStyle = hzd.kind === "mine" ? "#8cf7ff" : "#ff5555";
    ctx.beginPath();
    ctx.arc(x,y,3.5,0,Math.PI*2);
    ctx.fill();
  }

  for(const e of state.enemies){
    const x = ox + e.mesh.position.x*scale;
    const y = oy + e.mesh.position.z*scale;
    ctx.fillStyle =
      e.type==="elite" ? "#ff9b5f" :
      e.type==="logo" ? "#ffd24d" :
      e.type==="tank" ? "#ff799f" :
      e.type==="runner" ? "#a7ff52" :
      "#7fe7ff";
    ctx.beginPath();
    ctx.arc(x,y,e.type==="tank"?4.5:3.3,0,Math.PI*2);
    ctx.fill();
  }

  if(state.boss){
    const x = ox + state.boss.mesh.position.x*scale;
    const y = oy + state.boss.mesh.position.z*scale;
    ctx.strokeStyle = "#ff2b80";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(x,y,7,0,Math.PI*2);
    ctx.stroke();
  }

  ctx.save();
  ctx.translate(w/2,h/2);
  ctx.rotate(-lookYaw);
  ctx.fillStyle = "#ffffff";
  ctx.beginPath();
  ctx.moveTo(0,-8);
  ctx.lineTo(6,6);
  ctx.lineTo(-6,6);
  ctx.closePath();
  ctx.fill();
  ctx.restore();

  if(ui.minimapLabel){
    ui.minimapLabel.textContent = state.boss ? "BOSS IN ARENA" : `Hostiles ${state.enemies.length}`;
  }
}


  function queueNextWave(delay=1.2){
    if(state.nextWaveQueued || !player.alive) return;
    state.nextWaveQueued = true;
    setTimeout(() => {
      state.nextWaveQueued = false;
    state.moonNukeWave = 0;
    state.preBossRelief = false;
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

  const wave = player?.wave || 1;
  const combo = state?.combo || 1;
  const hpRatio = player?.maxHp ? player.hp / player.maxHp : 1;
  const bossActive = !!state.boss;

  // intensiteit bepaalt hoeveel lagen / agressie / snelheid
  let intensity = 0.32;
  intensity += Math.min(0.30, wave * 0.022);
  intensity += Math.min(0.18, Math.max(0, combo - 1) * 0.09);
  intensity += bossActive ? 0.32 : 0;
  intensity += hpRatio < 0.45 ? (0.45 - hpRatio) * 0.55 : 0;
  intensity = Math.max(0.2, Math.min(1.0, intensity));

  // tempo schaalt mee met spanning
  const stepDur =
    bossActive ? Math.max(0.092, 0.122 - intensity * 0.018)
    : hpRatio < 0.35 ? Math.max(0.098, 0.132 - intensity * 0.02)
    : Math.max(0.104, 0.145 - intensity * 0.024);

  const scheduleAhead = 0.34 + intensity * 0.12;

  const kick = (when, vol=0.018, heavy=false) => {
    if(!audioCtx) return;

    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    const filter = audioCtx.createBiquadFilter();

    osc.type = heavy ? "sawtooth" : "triangle";
    filter.type = "lowpass";
    filter.frequency.setValueAtTime(220, when);

    osc.frequency.setValueAtTime(heavy ? 120 : 95, when);
    osc.frequency.exponentialRampToValueAtTime(42, when + (heavy ? 0.12 : 0.09));

    gain.gain.setValueAtTime(0.0001, when);
    gain.gain.exponentialRampToValueAtTime(vol, when + 0.004);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + (heavy ? 0.16 : 0.11));

    osc.connect(filter).connect(gain).connect(audioCtx.destination);
    osc.start(when);
    osc.stop(when + 0.18);
  };

  const hat = (when, vol=0.004, bright=7000) => {
    if(!audioCtx) return;

    const size = Math.max(1, (audioCtx.sampleRate * 0.02) | 0);
    const buffer = audioCtx.createBuffer(1, size, audioCtx.sampleRate);
    const data = buffer.getChannelData(0);

    for(let i=0;i<size;i++){
      data[i] = (Math.random() * 2 - 1) * (1 - i / size);
    }

    const src = audioCtx.createBufferSource();
    const gain = audioCtx.createGain();
    const hp = audioCtx.createBiquadFilter();

    hp.type = "highpass";
    hp.frequency.setValueAtTime(bright, when);

    gain.gain.setValueAtTime(0.0001, when);
    gain.gain.exponentialRampToValueAtTime(vol, when + 0.002);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + 0.028);

    src.buffer = buffer;
    src.connect(hp).connect(gain).connect(audioCtx.destination);
    src.start(when);
    src.stop(when + 0.04);
  };

  const subDrop = (freq, when, dur, gainAmt) => {
    if(!audioCtx) return;

    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    const lp = audioCtx.createBiquadFilter();

    osc.type = "sine";
    lp.type = "lowpass";
    lp.frequency.setValueAtTime(180, when);

    osc.frequency.setValueAtTime(freq, when);
    osc.frequency.exponentialRampToValueAtTime(Math.max(28, freq * 0.6), when + dur);

    gain.gain.setValueAtTime(0.0001, when);
    gain.gain.exponentialRampToValueAtTime(gainAmt, when + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, when + dur);

    osc.connect(lp).connect(gain).connect(audioCtx.destination);
    osc.start(when);
    osc.stop(when + dur + 0.03);
  };

  while(state.songClock < t + scheduleAhead){
    state.songStep = (state.songStep + 1) % musicLead.length;
    const when = Math.max(t, state.songClock);
    const step = state.songStep;

    const lead = musicLead[step];
    const harmony = musicHarmony[step];
    const bass = musicBass[step];
    const arp = musicArp[step];

    const barPos = step % 8;
    const phrasePos = step % 16;
    const accent = barPos === 0;
    const danger = hpRatio < 0.35;
    const climax = combo >= 2.4 || bossActive;

    // melodische shift voor spanning
    const leadFreq =
      bossActive ? lead * (phrasePos >= 12 ? 0.5 : 1)
      : danger && phrasePos >= 8 ? lead * 0.5
      : lead;

    const harmonyFreq =
      bossActive && phrasePos % 4 === 2 ? harmony * 1.5
      : danger ? harmony * 0.75
      : harmony;

    // hoofdlead
    playChip(
      leadFreq,
      when,
      accent ? 0.24 : (climax ? 0.18 : 0.15),
      accent || climax ? "sawtooth" : "square",
      accent ? (0.030 + intensity * 0.010) : (0.018 + intensity * 0.010),
      bossActive ? -6 : 0
    );

    // onderste harmony laag
    playChip(
      harmonyFreq,
      when + 0.018,
      danger ? 0.18 : 0.14,
      danger ? "sawtooth" : "triangle",
      0.008 + intensity * 0.005,
      danger ? -8 : 0
    );

    // bass pulse
    playChip(
      bass,
      when,
      bossActive ? 0.22 : 0.18,
      "square",
      0.013 + intensity * 0.008,
      -10
    );

    // sub layer op accents / bosses
    if(accent || bossActive){
      subDrop(
        Math.max(45, bass * 0.5),
        when,
        bossActive ? 0.18 : 0.14,
        bossActive ? 0.018 : 0.012
      );
    }

    // arp
    if(step % 2 === 0){
      playChip(
        arp,
        when + 0.08,
        climax ? 0.09 : 0.075,
        "square",
        0.006 + intensity * 0.004,
        bossActive ? 8 : 4
      );
    }

    // extra upper sparkle bij hoge combo / latere waves
    if((combo >= 2 || wave >= 6) && step % 4 === 1){
      playChip(
        lead * 2,
        when + 0.045,
        0.06,
        "triangle",
        0.004 + intensity * 0.003,
        10
      );
    }

    // kick pattern
    if(barPos === 0 || barPos === 4){
      kick(when, 0.014 + intensity * 0.012, bossActive || accent);
    }
    if((intensity > 0.48 && barPos === 2) || (bossActive && barPos === 6)){
      kick(when, 0.010 + intensity * 0.008, false);
    }

    // hats
    if(step % 2 === 1){
      hat(when + 0.03, 0.0028 + intensity * 0.0035, bossActive ? 8200 : 7000);
    }
    if(intensity > 0.55){
      hat(when + stepDur * 0.5, 0.002 + intensity * 0.0028, 9000);
    }

    // snare/noise accent
    if(barPos === 3 || barPos === 7){
      noiseBurst(0.02 + intensity * 0.015, 0.003 + intensity * 0.0035);
    }

    // boss pulse / alarm laag
    if(bossActive && step % 4 === 0){
      playChip(
        Math.max(110, bass * 2),
        when + 0.01,
        0.11,
        "sawtooth",
        0.007 + intensity * 0.004,
        -16
      );
    }

    // low HP noodgevoel
    if(danger && step % 8 === 6){
      playChip(
        Math.max(220, lead * 0.5),
        when + 0.02,
        0.12,
        "sawtooth",
        0.008 + (0.35 - hpRatio) * 0.02,
        -18
      );
    }

    state.songClock += stepDur;
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
    weapon: "bullet",
    speedBoost: 0,
    speedBoostTimer: 0
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
    abilityFlashTimers: { plasma:null, mine:null, orbital:null },
    moonNukeWave: 0,
    waveSpawnToken: 0,
    preBossRelief: false
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

function makeNameTagSprite(text, glow="#00f7ff"){
  const canvas = document.createElement("canvas");
  canvas.width = 320;
  canvas.height = 96;
  const ctx = canvas.getContext("2d");

  ctx.clearRect(0,0,canvas.width,canvas.height);

  ctx.fillStyle = "rgba(6,10,22,.82)";
  ctx.strokeStyle = "rgba(255,255,255,.16)";
  ctx.lineWidth = 3;
  ctx.beginPath();
  ctx.roundRect(10, 14, 300, 54, 18);
  ctx.fill();
  ctx.stroke();

  ctx.shadowColor = glow;
  ctx.shadowBlur = 18;
  ctx.fillStyle = "#f7fbff";
  ctx.font = "900 28px system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 160, 42);

  const texture = new THREE.CanvasTexture(canvas);
  texture.needsUpdate = true;

  const material = new THREE.SpriteMaterial({
    map: texture,
    transparent: true,
    depthWrite: false
  });

  const sprite = new THREE.Sprite(material);
  sprite.scale.set(2.6, 0.78, 1);
  return sprite;
}

function makeEnemyMesh(type="basic", isBoss=false, profile=null){
  const variant = profile?.variant || "standard";
  const enemyName = profile?.name || "OH Unit";

  const palette = isBoss
    ? { suit:0x12335e, bodysuit:0x0a172b, cape:0x8d1230, trim:0xe6f8ff, glow:0xff5ea8 }
    : type === "elite"
      ? { suit:0x3e4fa3, bodysuit:0x141d42, cape:0x7d1230, trim:0xffdb92, glow:0xffb14d }
      : type === "runner"
        ? { suit:0x1e7cb7, bodysuit:0x10334f, cape:0x8b1937, trim:0xdff9ff, glow:0x7be7ff }
        : type === "tank"
          ? { suit:0x46515e, bodysuit:0x242b34, cape:0x5f1626, trim:0xffd37a, glow:0xff8d57 }
          : { suit:0x315c96, bodysuit:0x1d304f, cape:0x7a1631, trim:0xe8f7ff, glow:0x9fd8ef };

  const variantTint = {
    standard: 1,
    commander: 1.08,
    brute: 0.92,
    hunter: 1.04,
    phantom: 1.12,
    sentinel: 0.96,
    striker: 1.1,
    warden: 0.9,
    vanguard: 1.05
  }[variant] || 1;

  const scale =
    isBoss ? 2.02 :
    type === "tank" ? 1.22 :
    type === "elite" ? 1.12 :
    type === "runner" ? 0.98 : 1.04;

  const heightMul =
    isBoss ? 1.18 :
    variant === "brute" ? 0.94 :
    variant === "phantom" ? 1.06 :
    variant === "warden" ? 1.03 :
    1;

  const shoulderMul =
    isBoss ? 1.18 :
    type === "tank" ? 1.2 :
    variant === "brute" ? 1.12 :
    variant === "hunter" ? 0.96 :
    1;

  const legMul =
    type === "runner" ? 1.06 :
    variant === "phantom" ? 1.04 :
    variant === "brute" ? 0.95 :
    1;

  const headMul =
    variant === "commander" ? 1.05 :
    variant === "phantom" ? 0.96 :
    1;

  function matColor(hex, mul=1){
    const c = new THREE.Color(hex);
    c.multiplyScalar(mul);
    return c;
  }

  const group = new THREE.Group();

  const suitMat = new THREE.MeshStandardMaterial({
    color: matColor(palette.suit, variantTint),
    emissive: matColor(palette.bodysuit, isBoss ? 0.55 : 0.18),
    emissiveIntensity: isBoss ? 0.40 : 0.18,
    roughness: 0.56,
    metalness: 0.18
  });

  const bodysuitMat = new THREE.MeshStandardMaterial({
    color: matColor(palette.bodysuit, variantTint),
    roughness: 0.82,
    metalness: 0.08
  });

  const trimMat = new THREE.MeshStandardMaterial({
    color: matColor(palette.trim, 1.0),
    emissive: matColor(palette.trim, 0.35),
    emissiveIntensity: 0.18,
    roughness: 0.30,
    metalness: 0.32
  });

  const glowMat = new THREE.MeshStandardMaterial({
    color: matColor(palette.glow, 1.0),
    emissive: matColor(palette.glow, 0.9),
    emissiveIntensity: isBoss ? 0.72 : 0.46,
    roughness: 0.22,
    metalness: 0.38
  });

  const capeMat = new THREE.MeshStandardMaterial({
    color: matColor(palette.cape, 1.0),
    emissive: 0x230811,
    emissiveIntensity: 0.16,
    roughness: 0.76,
    metalness: 0.04,
    side: THREE.DoubleSide
  });

  const skinMat = new THREE.MeshStandardMaterial({
    color: 0xd9ab88,
    roughness: 0.9,
    metalness: 0.02
  });

  const pelvis = new THREE.Mesh(
    new THREE.BoxGeometry((type==="tank"?1.04:0.80)*shoulderMul, 0.40*heightMul, 0.36),
    bodysuitMat
  );
  pelvis.position.y = 0.98;

  const torso = new THREE.Mesh(
    new THREE.BoxGeometry((type==="tank"?1.18:0.90)*shoulderMul, 1.16*heightMul, 0.48),
    suitMat
  );
  torso.position.y = 1.80;

  const chestPlate = new THREE.Mesh(
    new THREE.BoxGeometry((type==="tank"?1.00:0.74)*shoulderMul, 0.82*heightMul, 0.18),
    trimMat
  );
  chestPlate.position.set(0, 1.88, 0.24);

  const waistBelt = new THREE.Mesh(
    new THREE.BoxGeometry((type==="tank"?1.04:0.86)*shoulderMul, 0.13, 0.22),
    glowMat
  );
  waistBelt.position.set(0, 1.26, 0.24);

  const chestLogo = makeLogoGlyph(type === "tank" ? 0.84 : 0.70);
  chestLogo.position.set(0, 1.88, 0.35);

  const backLogo = makeLogoGlyph(type === "tank" ? 0.72 : 0.60);
  backLogo.position.set(0, 1.80, -0.30);
  backLogo.rotation.y = Math.PI;

  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.12,0.12,0.18,12), skinMat);
  neck.position.y = 2.40 * heightMul;

  const head = new THREE.Mesh(
    new THREE.BoxGeometry(0.56*headMul, 0.66*headMul, 0.56*headMul),
    skinMat
  );
  head.position.y = 2.84 * heightMul;

  const hair = new THREE.Mesh(
    new THREE.BoxGeometry(0.58*headMul, 0.18, 0.58*headMul),
    bodysuitMat
  );
  hair.position.set(0, 3.10 * heightMul, 0.01);

  const visorGeo = variant === "phantom"
    ? new THREE.BoxGeometry(0.56, 0.16, 0.10)
    : new THREE.BoxGeometry(0.52, 0.18, 0.09);

  const mask = new THREE.Mesh(visorGeo, glowMat);
  mask.position.set(0, 2.86 * heightMul, 0.29);

  const jawGuard = new THREE.Mesh(new THREE.BoxGeometry(0.46, 0.14, 0.09), trimMat);
  jawGuard.position.set(0, 2.64 * heightMul, 0.28);

  const shoulderL = new THREE.Mesh(
    new THREE.BoxGeometry(0.36*shoulderMul, 0.24, 0.32),
    suitMat
  );
  const shoulderR = shoulderL.clone();
  shoulderL.position.set(-0.62*shoulderMul, 2.08*heightMul, 0.02);
  shoulderR.position.set( 0.62*shoulderMul, 2.08*heightMul, 0.02);

  const upperArmL = new THREE.Mesh(
    new THREE.BoxGeometry(0.24, 0.78*heightMul, 0.24),
    suitMat
  );
  const upperArmR = upperArmL.clone();
  upperArmL.position.set(-0.64*shoulderMul, 1.62*heightMul, 0.02);
  upperArmR.position.set( 0.64*shoulderMul, 1.62*heightMul, 0.02);

  const foreArmL = new THREE.Mesh(
    new THREE.BoxGeometry(0.22, 0.78*heightMul, 0.22),
    bodysuitMat
  );
  const foreArmR = foreArmL.clone();
  foreArmL.position.set(-0.64*shoulderMul, 0.88*heightMul, 0.05);
  foreArmR.position.set( 0.64*shoulderMul, 0.88*heightMul, 0.05);

  const gauntletL = new THREE.Mesh(new THREE.BoxGeometry(0.26,0.22,0.26), glowMat);
  const gauntletR = gauntletL.clone();
  gauntletL.position.set(-0.64*shoulderMul, 0.52*heightMul, 0.06);
  gauntletR.position.set( 0.64*shoulderMul, 0.52*heightMul, 0.06);

  const handL = new THREE.Mesh(new THREE.BoxGeometry(0.16,0.16,0.16), skinMat);
  const handR = handL.clone();
  handL.position.set(-0.64*shoulderMul, 0.38*heightMul, 0.06);
  handR.position.set( 0.64*shoulderMul, 0.38*heightMul, 0.06);

  const thighL = new THREE.Mesh(
    new THREE.BoxGeometry(type === "tank" ? 0.30 : 0.27, 0.86*legMul, 0.28),
    bodysuitMat
  );
  const thighR = thighL.clone();
  thighL.position.set(-0.24, 0.46, 0.03);
  thighR.position.set( 0.24, 0.46, 0.03);

  const kneeL = new THREE.Mesh(new THREE.BoxGeometry(0.25,0.15,0.30), glowMat);
  const kneeR = kneeL.clone();
  kneeL.position.set(-0.24, 0.06, 0.08);
  kneeR.position.set( 0.24, 0.06, 0.08);

  const shinL = new THREE.Mesh(
    new THREE.BoxGeometry(0.23, 0.78*legMul, 0.26),
    suitMat
  );
  const shinR = shinL.clone();
  shinL.position.set(-0.24, -0.28, 0.03);
  shinR.position.set( 0.24, -0.28, 0.03);

  const bootL = new THREE.Mesh(new THREE.BoxGeometry(0.32,0.20,0.50), bodysuitMat);
  const bootR = bootL.clone();
  bootL.position.set(-0.24,-0.76,0.10);
  bootR.position.set( 0.24,-0.76,0.10);

  const capeWidth =
    isBoss ? 1.24 :
    type === "tank" ? 1.18 :
    variant === "phantom" ? 0.82 :
    0.96;

  const capeHeight =
    isBoss ? 1.82 :
    type === "tank" ? 1.56 :
    variant === "hunter" ? 1.02 :
    1.30;

  const cape = new THREE.Mesh(
    new THREE.PlaneGeometry(capeWidth, capeHeight, 1, 7),
    capeMat
  );
  cape.position.set(0, 1.72, -0.30);
  cape.rotation.x = 0.08;

  const shoulderHolster = new THREE.Mesh(new THREE.BoxGeometry(0.18,0.36,0.09), trimMat);
  shoulderHolster.position.set(-0.30, 1.74, 0.28);

  const radio = new THREE.Mesh(new THREE.BoxGeometry(0.17,0.22,0.10), bodysuitMat);
  radio.position.set(0.30,1.60,0.28);

  const gun = new THREE.Group();

  if(type === "runner"){
    const smgBody = new THREE.Mesh(new THREE.BoxGeometry(0.14,0.12,0.56), bodysuitMat);
    const smgBarrel = new THREE.Mesh(new THREE.CylinderGeometry(0.022,0.022,0.54,12), trimMat);
    smgBarrel.rotation.x = Math.PI/2;
    smgBarrel.position.set(0,0,-0.38);
    const smgStock = new THREE.Mesh(new THREE.BoxGeometry(0.12,0.12,0.16), suitMat);
    smgStock.position.set(0,-0.02,0.24);
    const smgCore = new THREE.Mesh(new THREE.BoxGeometry(0.06,0.06,0.10), glowMat);
    smgCore.position.set(0.03,0.03,-0.05);
    gun.add(smgBody, smgBarrel, smgStock, smgCore);
    gun.position.set(0.20,1.30,0.30);
    gun.rotation.x = -0.18;
    gun.rotation.y = Math.PI/2;
  }else if(type === "tank"){
    const cannonBody = new THREE.Mesh(new THREE.BoxGeometry(0.24,0.20,0.92), bodysuitMat);
    const cannonBarrel = new THREE.Mesh(new THREE.CylinderGeometry(0.04,0.04,0.96,14), trimMat);
    cannonBarrel.rotation.x = Math.PI/2;
    cannonBarrel.position.set(0,0,-0.62);
    const cannonStock = new THREE.Mesh(new THREE.BoxGeometry(0.20,0.20,0.24), suitMat);
    cannonStock.position.set(0,-0.02,0.38);
    const cannonCore = new THREE.Mesh(new THREE.BoxGeometry(0.12,0.12,0.18), glowMat);
    cannonCore.position.set(0.05,0.05,-0.10);
    gun.add(cannonBody, cannonBarrel, cannonStock, cannonCore);
    gun.position.set(0.24,1.38,0.36);
    gun.rotation.x = -0.24;
    gun.rotation.y = Math.PI/2;
  }else if(type === "elite" || isBoss){
    const rifleBody = new THREE.Mesh(new THREE.BoxGeometry(0.18,0.15,0.84), bodysuitMat);
    const rifleBarrel = new THREE.Mesh(new THREE.CylinderGeometry(0.03,0.03,0.90,14), trimMat);
    rifleBarrel.rotation.x = Math.PI/2;
    rifleBarrel.position.set(0,0,-0.58);
    const rifleStock = new THREE.Mesh(new THREE.BoxGeometry(0.15,0.15,0.24), suitMat);
    rifleStock.position.set(0,-0.02,0.36);
    const rifleCore = new THREE.Mesh(new THREE.BoxGeometry(0.08,0.08,0.14), glowMat);
    rifleCore.position.set(0.04,0.04,-0.08);
    const scope = new THREE.Mesh(new THREE.BoxGeometry(0.10,0.08,0.18), glowMat);
    scope.position.set(0,0.12,-0.02);
    gun.add(rifleBody, rifleBarrel, rifleStock, rifleCore, scope);
    gun.position.set(0.22,1.36,0.34);
    gun.rotation.x = -0.22;
    gun.rotation.y = Math.PI/2;
  }else{
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
  }

  const antenna = new THREE.Mesh(new THREE.BoxGeometry(0.02,0.24,0.02), trimMat);
  antenna.position.set(0.18, 3.06 * heightMul, -0.08);
  const antennaTip = new THREE.Mesh(new THREE.SphereGeometry(0.04, 8, 8), glowMat);
  antennaTip.position.set(0.18, 3.20 * heightMul, -0.08);

  const shoulderBadgeL = makeLogoGlyph(0.24);
  const shoulderBadgeR = makeLogoGlyph(0.24);
  shoulderBadgeL.position.set(-0.64*shoulderMul, 2.08*heightMul, 0.18);
  shoulderBadgeL.rotation.y = -0.35;
  shoulderBadgeR.position.set(0.64*shoulderMul, 2.08*heightMul, 0.18);
  shoulderBadgeR.rotation.y = 0.35;

  const spineLight = new THREE.Mesh(new THREE.BoxGeometry(0.12,0.74,0.08), glowMat);
  spineLight.position.set(0,1.64,-0.18);

  [
    pelvis, torso, chestPlate, waistBelt, chestLogo, backLogo, neck, head, hair, mask, jawGuard,
    shoulderL, shoulderR, upperArmL, upperArmR, foreArmL, foreArmR, gauntletL, gauntletR,
    handL, handR, thighL, thighR, kneeL, kneeR, shinL, shinR, bootL, bootR, cape,
    shoulderHolster, radio, gun, antenna, antennaTip, shoulderBadgeL, shoulderBadgeR, spineLight
  ].forEach(m => {
    m.castShadow = true;
    m.receiveShadow = true;
    group.add(m);
  });

  if(isBoss){
    const crest = new THREE.Mesh(new THREE.TorusGeometry(1.12,.09,14,42), glowMat);
    crest.rotation.x = Math.PI/2;
    crest.position.y = 3.62;
    group.add(crest);
  }

  const tag = makeNameTagSprite(enemyName, isBoss ? "#ff5ea8" : type === "elite" ? "#ffb14d" : "#00f7ff");
  tag.position.set(0, isBoss ? 4.8 : 4.0, 0);
  group.add(tag);

  group.userData.parts = {
    armL: upperArmL,
    armR: upperArmR,
    foreArmL,
    foreArmR,
    legL: thighL,
    legR: thighR,
    shinL,
    shinR,
    gun,
    logo: chestLogo,
    backLogo,
    hatBadge: mask,
    head,
    torso,
    pelvis,
    bootL,
    bootR,
    chest: chestPlate,
    cape,
    gauntletL,
    gauntletR,
    nameTag: tag
  };

  group.userData.enemyName = enemyName;
  group.userData.variant = variant;
  group.userData.ohStamped = true;
  group.scale.setScalar(scale);
  return group;
}

function waveRamp(wave = player.wave || 1){
  const early = Math.min(5, Math.max(0, wave - 1)) * 0.045;
  const mid = Math.min(6, Math.max(0, wave - 6)) * 0.085;
  const late = Math.max(0, wave - 12) * 0.13;
  return 1 + early + mid + late;
}

function bossPressure(wave = player.wave || 1){
  return 1 + Math.max(0, wave - 4) * 0.09 + Math.max(0, wave - 10) * 0.07;
}

function bossWaveInterval(wave = player.wave || 1){
  if(wave >= 15) return 2;
  if(wave >= 9) return 3;
  return 4;
}

function isBossWave(wave = player.wave || 1){
  return wave >= 3 && wave % bossWaveInterval(wave) === 0;
}

function spawnEnemy(isBoss=false){
  function findSafeEnemySpawn(radius){
    const minPlayerDist = isBoss ? 18 : 14;
    const spawnMin = -48;
    const spawnMax = 48;

    function enemySpawnBoxCollides(x, z, radius, height = 3){
  const box = new THREE.Box3(
    new THREE.Vector3(x - radius, 0, z - radius),
    new THREE.Vector3(x + radius, height, z + radius)
  );

  for(const c of colliders){
    if(box.intersectsBox(c.box)) return true;
  }
  return false;
}

    function isSafeSpawn(x, z, r){
      // niet te dicht bij speler
      const dx = x - player.pos.x;
      const dz = z - player.pos.z;
      if(Math.hypot(dx, dz) < minPlayerDist) return false;

      // extra veiligheidsmarge t.o.v. map blocks
      if(collidesAt(x, z, r)) return false;
    if(collidesAt(x + r * 0.75, z, r * 0.65)) return false;
    if(collidesAt(x - r * 0.75, z, r * 0.65)) return false;
    if(collidesAt(x, z + r * 0.75, r * 0.65)) return false;
    if(collidesAt(x, z - r * 0.75, r * 0.65)) return false;

    // extra harde check tegen complete collider-boxen
    if(enemySpawnBoxCollides(x, z, r + 0.15, 3.2)) return false;

      // ook niet te dicht op andere enemies
      for(const other of state.enemies){
        if(!other?.mesh) continue;
        const ddx = x - other.mesh.position.x;
        const ddz = z - other.mesh.position.z;
        const minDist = r + (other.radius || 1) + 1.1;
        if(Math.hypot(ddx, ddz) < minDist) return false;
      }

      // ook niet op boss
      if(state.boss?.mesh){
        const bdx = x - state.boss.mesh.position.x;
        const bdz = z - state.boss.mesh.position.z;
        const minBossDist = r + (state.boss.radius || 1.8) + 1.4;
        if(Math.hypot(bdx, bdz) < minBossDist) return false;
      }

      return true;
    }

    // 1. random pogingen
    for(let tries = 0; tries < 120; tries++){
      const x = rand(spawnMin, spawnMax);
      const z = rand(spawnMin, spawnMax);
      if(isSafeSpawn(x, z, radius)) return { x, z };
    }

    // 2. fallback: systematisch in ringen zoeken rond speler
    for(let ring = 16; ring <= 52; ring += 3){
      for(let i = 0; i < 40; i++){
        const a = (i / 40) * Math.PI * 2;
        const x = player.pos.x + Math.cos(a) * ring;
        const z = player.pos.z + Math.sin(a) * ring;

        if(x < spawnMin || x > spawnMax || z < spawnMin || z > spawnMax) continue;
        if(isSafeSpawn(x, z, radius)) return { x, z };
      }
    }

    return null;
  }

  let type = "basic";
  if(!isBoss){
    const roll = Math.random();
    const wave = player.wave || 1;
    const eliteChance = wave >= 12 ? 0.28 : wave >= 8 ? 0.21 : wave >= 7 ? 0.16 : 0;
    const tankChance = wave >= 10 ? 0.56 : wave >= 6 ? 0.46 : wave >= 4 ? 0.38 : 0;
    const runnerChance = wave >= 14 ? 0.82 : wave >= 9 ? 0.76 : wave >= 2 ? 0.68 : 0;

    if(roll < eliteChance) type = "elite";
    else if(roll < tankChance) type = "tank";
    else if(roll < runnerChance) type = "runner";
  }

  const radius =
    isBoss ? 1.8 :
    type === "tank" ? 1.2 :
    type === "elite" ? 1.08 :
    1.0;

  const spawn = findSafeEnemySpawn(radius + 0.35);
  if(!spawn){
    console.warn("Geen veilige enemy spawn gevonden");
    return null;
  }

  const mesh = makeEnemyMesh(type, isBoss);
  mesh.position.set(spawn.x, 0, spawn.z);
  scene.add(mesh);

  const waveScale = waveRamp(player.wave);
  const bossScale = bossPressure(player.wave);
  const baseHp = isBoss
    ? Math.round((250 + player.wave * 34 + Math.max(0, player.wave - 8) * 22) * bossScale)
    : type === "runner"
    ? Math.round((16 + player.wave * 3.4 + Math.max(0, player.wave - 8) * 1.8) * (0.96 + (waveScale - 1) * 0.55))
    : type === "tank"
    ? Math.round((44 + player.wave * 7.5 + Math.max(0, player.wave - 7) * 3.0) * (1.02 + (waveScale - 1) * 0.82))
    : type === "elite"
    ? Math.round((54 + player.wave * 8.0 + Math.max(0, player.wave - 7) * 3.4) * (1.05 + (waveScale - 1) * 0.88))
    : type === "logo"
    ? Math.round((34 + player.wave * 5.6 + Math.max(0, player.wave - 8) * 2.1) * (1 + (waveScale - 1) * 0.65))
    : Math.round((24 + player.wave * 4.8 + Math.max(0, player.wave - 6) * 1.8) * (1 + (waveScale - 1) * 0.72));

  const ringGeo = new THREE.RingGeometry(
    isBoss ? 1.8 : 0.78,
    isBoss ? 2.02 : 0.92,
    40
  );
  const ringMat = new THREE.MeshBasicMaterial({
    color: isBoss ? 0xff8aa7 : (type === "elite" ? 0xffd166 : 0x7dd8ff),
    transparent: true,
    opacity: isBoss ? 0.34 : 0.20,
    side: THREE.DoubleSide
  });

  const groundRing = new THREE.Mesh(ringGeo, ringMat);
  groundRing.rotation.x = -Math.PI / 2;
  groundRing.position.set(spawn.x, 0.03, spawn.z);
  scene.add(groundRing);

  const enemy = {
    type,
    isBoss,
    mesh,
    groundRing,
    hp: baseHp,
    maxHp: baseHp,
    speed: isBoss
      ? 3.0 + Math.min(2.0, (bossScale - 1) * 0.9)
      : type === "runner"
      ? 5.4 + player.wave * 0.16 + Math.max(0, player.wave - 9) * 0.05
      : type === "tank"
      ? 2.3 + player.wave * 0.075 + Math.max(0, player.wave - 10) * 0.03
      : type === "elite"
      ? 4.0 + player.wave * 0.12 + Math.max(0, player.wave - 8) * 0.04
      : 3.3 + player.wave * 0.11 + Math.max(0, player.wave - 10) * 0.035,
    radius,
    fireCooldown: isBoss ? 0.95 : rand(0.9, 2.2),
    strafe: rand(-1, 1),
    bob: rand(0, Math.PI * 2)
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

  return enemy;
}


function spawnWave(){
  state.lastClearStamp = performance.now();

  const wave = player.wave;
  const pressure = waveRamp(wave);
  const bossWave = isBossWave(wave);
  const spawnToken = ++state.waveSpawnToken;
  const baseCount = Math.min(5 + Math.floor(wave * 1.9 + Math.max(0, wave - 6) * 0.9), 52);

  const batchCount =
    wave < 3 ? 1 :
    wave < 6 ? 2 :
    wave < 10 ? 3 :
    wave < 15 ? 4 : 5;

  const totalCount = Math.min(
    Math.round(baseCount + (wave >= 8 ? 2 : 0) + (bossWave ? 2 + Math.floor(Math.max(0, wave - 9) / 3) : 0) + (pressure - 1) * 4),
    58
  );
  const perBatch = Math.max(2, Math.ceil(totalCount / batchCount));

  const waveLabel =
    bossWave && wave >= 12 ? `WAVE ${wave} · DIRECTOR OVERRUN` :
    bossWave ? `WAVE ${wave} · BOSS WAVE` :
    wave >= 10 ? `WAVE ${wave} · ARENA LOCKDOWN` :
    wave >= 6 ? `WAVE ${wave} · HEAVY CONTACT` :
    `WAVE ${wave} · ${totalCount} vijanden`;

  showFloating(waveLabel);

  createShockwave(player.pos.clone(), bossWave ? 0xff6ea1 : 0x00f7ff);

  for(let batch = 0; batch < batchCount; batch++){
    const delay = batch * (wave >= 12 ? 480 : wave >= 8 ? 620 : 900);

    setTimeout(() => {
      if(!state.running || !player.alive) return;

      const remaining = totalCount - batch * perBatch;
      const amount = Math.max(0, Math.min(perBatch, remaining));

      for(let i = 0; i < amount; i++){
        setTimeout(() => {
          if(state.running && player.alive && state.waveSpawnToken === spawnToken) spawnEnemy(false);
        }, i * (wave >= 12 ? 80 : 110));
      }
    }, delay);
  }

  if(bossWave){
    setTimeout(() => {
      if(!state.running || !player.alive || state.boss || state.waveSpawnToken !== spawnToken) return;

      showFloating(wave >= 12 ? "ENRAGED BOSS INBOUND" : "BOSS INBOUND");
      createShockwave(player.pos.clone(), 0xff2e88);
      spawnEnemy(true);

      if(wave >= 12){
        setTimeout(() => {
          if(state.running && player.alive && state.boss){
            for(let i=0;i<Math.min(2 + Math.floor((wave - 12) / 4), 4); i++){
              setTimeout(() => {
                if(state.running && player.alive && state.boss) spawnEnemy(false);
              }, i * 160);
            }
          }
        }, 1700);
      }
    }, wave >= 12 ? 1050 : 1400);
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
      
    if(!Number.isFinite(e.hp)){
          console.warn("Enemy hp corrupt before damage", e);
          e.hp = Number.isFinite(e.maxHp) ? e.maxHp : 1;
        }
        if(!Number.isFinite(e.maxHp) || e.maxHp <= 0){
          console.warn("Enemy maxHp corrupt", e);
          e.maxHp = Math.max(1, Number.isFinite(e.hp) ? e.hp : 1);
        }

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

  const combo = state.combo || 1;

  const dir = dirOverride ? dirOverride.clone().normalize() : new THREE.Vector3();
  if(!dirOverride){
    camera.getWorldDirection(dir);
    dir.normalize();
  }

  // lichte aim assist richting dichtbijzijnde vijand in de kijkrichting
  const aimAssist = (() => {
    let best = null;
    let bestScore = 0.965; // hoe hoger, hoe strakker op de crosshair
    const from = player.pos.clone();
    from.y = 1.52;

    const candidates = [];
    if(state.boss?.mesh) candidates.push(state.boss);
    for(const e of state.enemies) candidates.push(e);

    for(const enemy of candidates){
      if(!enemy?.mesh) continue;
      const target = enemy.mesh.position.clone();
      target.y += enemy.isBoss ? 2.0 : 1.2;

      const toEnemy = target.clone().sub(from);
      const dist = toEnemy.length();
      if(dist < 1 || dist > 28) continue;

      const nd = toEnemy.normalize();
      const dot = dir.dot(nd);
      if(dot > bestScore){
        bestScore = dot;
        best = { dir: nd, dist, dot, enemy };
      }
    }

    if(!best) return dir;

    const strengthBase =
      weapon === "bullet" ? 0.10 :
      weapon === "rocket" ? 0.16 : 0.13;

    const nearBoost = best.dist < 12 ? 1.0 : 0.65;
    return dir.clone().lerp(best.dir, strengthBase * nearBoost).normalize();
  })();

  const finalDir = aimAssist.clone();

  const start = player.pos.clone();
  start.y = 1.52;
  start.add(finalDir.clone().multiplyScalar(.9));

  const right = new THREE.Vector3(finalDir.z, 0, -finalDir.x).normalize();
  const up = new THREE.Vector3(0, 1, 0);

  const makeDir = (yawOffset=0, pitchOffset=0) => {
    return finalDir.clone()
      .addScaledVector(right, yawOffset)
      .addScaledVector(up, pitchOffset)
      .normalize();
  };

  const spawnFriendly = (spawnPos, shotDir, opts) => {
    state.bullets.push(createProjectile(spawnPos, shotDir, {
      friendly: true,
      ...opts
    }));
  };

  const recoilBase =
    weapon === "bullet" ? 0.34 :
    weapon === "rocket" ? 0.85 : 0.62;

  const shakeBase =
    weapon === "bullet" ? 0.12 :
    weapon === "rocket" ? 0.45 : 0.26;

  state.viewKick = Math.min(
    1.5,
    state.viewKick + recoilBase * (weapon === "bullet" ? (1 + Math.min(.22, combo * .04)) : 1)
  );

  state.cameraShake = Math.min(
    1.8,
    state.cameraShake + shakeBase * (weapon === "bullet" ? 1 : 1 + Math.min(.14, combo * .03))
  );

  if(weaponRig.userData.flash){
    weaponRig.userData.flash.intensity =
      weapon === "bullet"
        ? 2.8 + Math.min(1.4, combo * 0.22)
        : weapon === "rocket"
        ? 4.8
        : 4.2;
  }

  if(weapon === "bullet"){
    player.ammo.bullet -= 1;

    const spread = Math.max(0.004, 0.018 - combo * 0.0025);
    const damageMain = 10 + Math.min(6, Math.floor(combo * 1.4));
    const lifeMain = 2.2 + Math.min(0.4, combo * 0.06);

    spawnFriendly(start.clone(), makeDir(
      (Math.random() - 0.5) * spread,
      (Math.random() - 0.5) * spread * 0.6
    ), {
      speed: 31 + Math.min(4, combo * 0.7),
      color: 0xffec7d,
      trailColor: 0xfff7bf,
      size: 0.12,
      life: lifeMain,
      damage: damageMain,
      type: "bullet"
    });

    // bij hogere combo: twin side shots
    if(combo >= 1.8){
      const sideDamage = Math.max(6, Math.round(damageMain * 0.58));
      const sideSpread = 0.085;

      [0.18, -0.18].forEach((offset, idx) => {
        const sideStart = start.clone().addScaledVector(right, offset);
        const sideDir = makeDir(
          idx === 0 ? sideSpread : -sideSpread,
          (Math.random() - 0.5) * 0.01
        );

        spawnFriendly(sideStart, sideDir, {
          speed: 29,
          color: 0x8bf0ff,
          trailColor: 0xdffcff,
          size: 0.09,
          life: 1.35,
          damage: sideDamage,
          type: "bullet"
        });
      });
    }

    // power tracer op echt hoge combo
    if(combo >= 3.0){
      const heavyDir = makeDir(0, 0);
      spawnFriendly(start.clone(), heavyDir, {
        speed: 34,
        color: 0xffffff,
        trailColor: 0xff9af2,
        size: 0.14,
        life: 1.9,
        damage: damageMain + 5,
        type: "bullet"
      });
      state.cameraShake = Math.min(2.0, state.cameraShake + 0.08);
    }

    player.fireCooldown = Math.max(0.115, 0.18 - Math.min(0.05, combo * 0.012));
    sfxShoot();

  } else if(weapon === "rocket"){
    player.ammo.rocket -= 1;

    const rocketDir = makeDir(0, 0);
    if(state.moonNukeWave !== player.wave && rocketAimTriggersMoonNuke(start.clone(), rocketDir.clone())){
      triggerMoonNuke(start.clone().add(rocketDir.clone().multiplyScalar(4)));
      state.cameraShake = Math.min(2.4, state.cameraShake + 0.55);
      player.fireCooldown = Math.max(0.42, 0.55 - Math.min(0.09, combo * 0.018));
      sfxRocket();
      setStat();
      return true;
    }

    spawnFriendly(start.clone(), rocketDir, {
      speed: 18 + Math.min(2.5, combo * 0.35),
      color: 0xff7b7b,
      trailColor: 0xffb0a3,
      smoke: true,
      size: 0.18,
      life: 2.6,
      damage: 28 + Math.min(10, Math.floor(combo * 2.2)),
      radius: 4.2 + Math.min(1.2, combo * 0.22),
      type: "rocket",
      explosionColor: 0xff7b7b
    });

    // bonus micro-rocket bij sterke combo
    if(combo >= 2.4){
      const microDir = makeDir((Math.random() > 0.5 ? 0.07 : -0.07), 0.01);
      const microStart = start.clone().addScaledVector(right, Math.random() > 0.5 ? 0.22 : -0.22);

      spawnFriendly(microStart, microDir, {
        speed: 19,
        color: 0xffd166,
        trailColor: 0xffefb0,
        smoke: true,
        size: 0.12,
        life: 1.8,
        damage: 12 + Math.min(5, Math.floor(combo)),
        radius: 2.2,
        type: "rocket",
        explosionColor: 0xffd166
      });
    }

    player.fireCooldown = Math.max(0.42, 0.55 - Math.min(0.09, combo * 0.018));
    sfxRocket();

  } else if(weapon === "grenade"){
    player.ammo.grenade -= 1;

    spawnFriendly(start.clone(), makeDir(0, 0.04), {
      speed: 14 + Math.min(1.6, combo * 0.25),
      color: 0x9dff7c,
      trailColor: 0xd8ffca,
      smoke: true,
      size: 0.16,
      life: 1.6,
      damage: 22 + Math.min(8, Math.floor(combo * 1.7)),
      radius: 3.6 + Math.min(1.2, combo * 0.24),
      type: "grenade",
      gravity: 10,
      explosionColor: 0x9dff7c
    });

    // grenade krijgt shrapnel-support op combo
    if(combo >= 2.0){
      const shardDamage = 5 + Math.min(4, Math.floor(combo));
      [-0.12, 0.12].forEach(offset => {
        const shardDir = makeDir(offset, 0.02);
        const shardStart = start.clone().addScaledVector(right, offset * 1.3);

        spawnFriendly(shardStart, shardDir, {
          speed: 24,
          color: 0xcaff9d,
          trailColor: 0xf0ffd8,
          size: 0.08,
          life: 1.0,
          damage: shardDamage,
          type: "bullet"
        });
      });
    }

    player.fireCooldown = Math.max(0.5, 0.65 - Math.min(0.1, combo * 0.02));
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
    const wave = player.wave || 1;
    const waveScale = waveRamp(wave);
    const bossScale = bossPressure(wave);

    let speed = enemy.isBoss ? 18 : 12;
    let color = enemy.isBoss ? 0xff6ea1 : 0x78d7ff;
    let damage = enemy.isBoss ? 16 : 11;
    if(enemy.type === "runner"){ speed = 14; damage = 9; }
    if(enemy.type === "tank"){ speed = 10; damage = 14; color = 0xffd166; }
    if(enemy.type === "elite"){ speed = 13; damage = 15; color = 0xffa86e; }

    if(enemy.isBoss){
      speed *= Math.min(1.65, 1 + (bossScale - 1) * 0.28);
      damage = Math.round(damage * Math.min(2.15, 1 + (bossScale - 1) * 0.42));
    } else {
      speed *= Math.min(1.4, 1 + (waveScale - 1) * 0.16);
      damage = Math.round(damage * Math.min(1.9, 1 + (waveScale - 1) * 0.22));
    }

    const burstCount = enemy.isBoss
      ? (wave >= 15 ? 4 : wave >= 9 ? 3 : 2)
      : (enemy.type === "runner" ? 1 : enemy.type === "elite" ? (wave >= 10 ? 3 : 2) : enemy.type === "tank" && wave >= 12 ? 2 : 1);
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
      ui.center.querySelector("p").textContent = "Je score is opgeslagen in de online leaderboard.";
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
  const BULLET_WALL_RADIUS = 0.14;
  const PLAYER_HIT_RADIUS = 1.15;

  function ensureEnemyHp(target){
    if(!target) return false;

    if(!Number.isFinite(target.maxHp) || target.maxHp <= 0){
      const fallbackMax = Number.isFinite(target.hp) && target.hp > 0 ? target.hp : 1;
      target.maxHp = fallbackMax;
    }

    if(!Number.isFinite(target.hp)){
      target.hp = target.maxHp;
    }

    target.hp = Math.min(target.hp, target.maxHp);
    return true;
  }

  function hitNormalEnemy(enemy, damage, hitPosition){
    if(!enemy?.mesh) return false;
    if(!ensureEnemyHp(enemy)) return false;

    const dmg = Math.max(0, Number.isFinite(damage) ? damage : 0);
    enemy.hp = Math.max(0, enemy.hp - dmg);

    createBurst(hitPosition, 0xffec7d, 6, 3);
    sfxHit();

    if(enemy.hp <= 0){
      killEnemy(enemy);
      const idx = state.enemies.indexOf(enemy);
      if(idx !== -1) state.enemies.splice(idx, 1);
    }

    return true;
  }

  function hitBoss(damage, hitPosition){
    if(!state.boss?.mesh) return false;
    if(!ensureEnemyHp(state.boss)) return false;

    const dmg = Math.max(0, Number.isFinite(damage) ? damage : 0);
    state.boss.hp = Math.max(0, state.boss.hp - dmg);

    createBurst(hitPosition, 0xff88bb, 8, 3);
    sfxHit();
    updateBossBar();

    if(state.boss.hp <= 0){
      killEnemy(state.boss);
    }

    return true;
  }

  function triggerMoonNuke(hitPosition=null){
    if(state.moonNukeWave === player.wave) return false;
    state.moonNukeWave = player.wave;
    state.waveSpawnToken += 1;
    state.nextWaveQueued = false;

    const epicenter = moon.position.clone();
    createFlash?.(epicenter.clone(), 0xdbe9ff, 4.8, 26, 0.34);
    createShockwave?.(epicenter.clone(), 0xc8dcff, 8.6);
    createShockwave?.(player.pos.clone(), 0xc8dcff, 5.8);
    createBurst?.(epicenter.clone(), 0xe7f1ff, 20, 5.8, { minLife:.16, maxLife:.34, minSize:.10, maxSize:.22 });

    if(hitPosition){
      createBurst?.(hitPosition.clone(), 0xffd8b0, 14, 3.8, { minLife:.12, maxLife:.24, minSize:.05, maxSize:.12 });
    }

    const allEnemies = state.enemies.slice();
    for(const enemy of allEnemies){
      damageEnemyDirect?.(enemy, 999999);
    }
    if(state.boss){
      damageEnemyDirect?.(state.boss, 999999);
    }

    for(let i = state.enemyBullets.length - 1; i >= 0; i--){
      removeEnemyBullet?.(i);
    }
    for(let i = state.bullets.length - 1; i >= 0; i--){
      const bullet = state.bullets[i];
      if(bullet?.mesh && bullet.type !== "rocket") scene.remove(bullet.mesh);
      if(bullet?.type !== "rocket") state.bullets.splice(i, 1);
    }

    state.enemies.length = 0;
    if(state.boss?.mesh){
      scene.remove(state.boss.mesh);
    }
    state.boss = null;
    ui.bossBarWrap?.classList.remove("show");
    updateBossBar?.();

    state.lastClearStamp = performance.now();
    flashHint?.("Moon nuke geactiveerd — volgende wave", 1800);
    apocToast?.("MOON NUKE");
    queueNextWave?.(0.3);
    return true;
  }

  function rocketAimTriggersMoonNuke(start, dir){
    if(!start || !dir || !moon?.position) return false;

    const aim = dir.clone().normalize();
    if(aim.y < 0.2) return false;

    const toMoon = moon.position.clone().sub(start);
    const moonDistance = toMoon.length();
    if(moonDistance <= 0.001) return false;

    const moonDir = toMoon.clone().normalize();
    const alignment = aim.dot(moonDir);

    // Strakker dan voorheen: alleen bijna exact op de maan mikken.
    if(alignment < 0.9965) return false;

    const projected = Math.max(0, toMoon.dot(aim));
    const closestPoint = start.clone().add(aim.clone().multiplyScalar(projected));
    const missDistance = closestPoint.distanceTo(moon.position);

    // Kleinere tolerantie zodat normale rockets niet onbedoeld als moon shot tellen.
    const allowedMiss = Math.max(3.4, moonDistance * 0.0125);

    return missDistance <= allowedMiss || alignment >= 0.9992;
  }

  function explodeProjectile(bullet){
    explodeAt(
      bullet.mesh.position.clone(),
      bullet.radius,
      bullet.damage,
      bullet.explosionColor
    );
  }

  function isExplosiveBullet(bullet){
    return bullet.type === "rocket" || bullet.type === "grenade" || bullet.type === "plasma";
  }

  function removePlayerBullet(index){
    const bullet = state.bullets[index];
    if(!bullet) return;
    if(bullet.mesh) scene.remove(bullet.mesh);
    state.bullets.splice(index, 1);
  }

  function removeEnemyBullet(index){
    const bullet = state.enemyBullets[index];
    if(!bullet) return;
    if(bullet.mesh) scene.remove(bullet.mesh);
    state.enemyBullets.splice(index, 1);
  }

  for(let i = state.bullets.length - 1; i >= 0; i--){
    const b = state.bullets[i];
    if(!b?.mesh){
      state.bullets.splice(i, 1);
      continue;
    }

    b.mesh.position.addScaledVector(b.vel, dt);

    if(b.gravity){
      b.vel.y -= b.gravity * dt;
    }

    b.life -= dt;
    let remove = b.life <= 0;

    // maan-hit met rocket = wave skip / nuke
    if(!remove && b.type === "rocket" && state.moonNukeWave !== player.wave){
      const moonHitRadius = 8.4;
      if(b.mesh.position.distanceTo(moon.position) <= moonHitRadius){
        triggerMoonNuke(b.mesh.position.clone());
        remove = true;
      }
    }

    // mikken op de maan mag ook zonder letterlijk de maanmesh te raken
    if(!remove && b.type === "rocket" && state.moonNukeWave !== player.wave){
      const flightDir = b.vel?.clone?.().normalize?.() || null;
      if(flightDir && rocketAimTriggersMoonNuke(b.mesh.position.clone(), flightDir)){
        triggerMoonNuke(b.mesh.position.clone());
        remove = true;
      }
    }

    // muur / arena collision
    if(!remove && collidesAt(b.mesh.position.x, b.mesh.position.z, BULLET_WALL_RADIUS)){
      if(isExplosiveBullet(b)){
        explodeProjectile(b);
      } else {
        createBurst(b.mesh.position, b.explosionColor, 6, 2.5);
      }
      remove = true;
    }

    // grond impact voor arcing projectiles
    if(!remove && b.mesh.position.y <= 0.2 && (b.type === "grenade" || b.type === "plasma")){
      explodeProjectile(b);
      remove = true;
    }

    // normale enemies
    if(!remove){
      for(let j = state.enemies.length - 1; j >= 0; j--){
        const e = state.enemies[j];
        if(!e?.mesh) continue;

        const hitPos = e.mesh.position.clone();
        hitPos.y = e.isBoss ? 2.5 : 1.9;

        const radius = Math.max(
          0.35,
          Number.isFinite(e.radius) ? e.radius : 1
        );

        if(b.mesh.position.distanceTo(hitPos) < radius){
          if(isExplosiveBullet(b)){
            explodeProjectile(b);
          } else {
            hitNormalEnemy(e, b.damage, b.mesh.position);
          }
          remove = true;
          break;
        }
      }
    }

    // boss
    if(!remove && state.boss?.mesh){
      const bossHitPos = state.boss.mesh.position.clone();
      bossHitPos.y = 2.5;

      const bossRadius = Math.max(
        0.8,
        Number.isFinite(state.boss.radius) ? state.boss.radius : 2
      );

      if(b.mesh.position.distanceTo(bossHitPos) < bossRadius){
        if(isExplosiveBullet(b)){
          explodeProjectile(b);
        } else {
          hitBoss(b.damage, b.mesh.position);
        }
        remove = true;
      }
    }

    if(remove){
      removePlayerBullet(i);
    }
  }

  for(let i = state.enemyBullets.length - 1; i >= 0; i--){
    const b = state.enemyBullets[i];
    if(!b?.mesh){
      state.enemyBullets.splice(i, 1);
      continue;
    }

    b.mesh.position.addScaledVector(b.vel, dt);
    b.life -= dt;

    let remove = b.life <= 0;

    if(!remove && collidesAt(b.mesh.position.x, b.mesh.position.z, BULLET_WALL_RADIUS)){
      remove = true;
    }

    if(!remove){
      const playerHit = new THREE.Vector3(player.pos.x, 1.45, player.pos.z);
      if(b.mesh.position.distanceTo(playerHit) < PLAYER_HIT_RADIUS){
        applyDamage(b.damage);
        createBurst(b.mesh.position, 0xff6ea1, 7, 2.8);
        remove = true;
      }
    }

    if(remove){
      removeEnemyBullet(i);
    }
  }
}

 function updateEnemies(dt){
  const now = performance.now() * 0.001;

  for(let i=state.enemies.length-1;i>=0;i--){
    const e = state.enemies[i];

    if(e.aiClock == null) e.aiClock = Math.random() * 10;
    if(e.dodgeCooldown == null) e.dodgeCooldown = rand(0.4, 1.4);
    if(e.burstCooldown == null) e.burstCooldown = rand(1.2, 2.8);
    if(e.chargeCooldown == null) e.chargeCooldown = rand(2.5, 5.0);
    if(e.wobble == null) e.wobble = rand(0.7, 1.3);

    e.aiClock += dt;
    e.fireCooldown -= dt;
    e.dodgeCooldown -= dt;
    e.burstCooldown -= dt;
    e.chargeCooldown -= dt;
    e.bob += dt * (e.type === "runner" ? 7.2 : e.type === "tank" ? 3.2 : e.type === "elite" ? 5.2 : 4.4);

    if(e.mesh.userData.parts){
      const swing = Math.sin(e.bob) * (
        e.type === "runner" ? 0.75 :
        e.type === "tank" ? 0.18 :
        e.type === "elite" ? 0.46 : 0.34
      );
      e.mesh.userData.parts.armL.rotation.x = swing * 0.52;
      e.mesh.userData.parts.armR.rotation.x = -swing * 0.48;
      e.mesh.userData.parts.legL.rotation.x = -swing;
      e.mesh.userData.parts.legR.rotation.x = swing;
      if(e.mesh.userData.parts.foreArmL) e.mesh.userData.parts.foreArmL.rotation.x = swing * 0.35;
      if(e.mesh.userData.parts.foreArmR) e.mesh.userData.parts.foreArmR.rotation.x = -swing * 0.35;
      if(e.mesh.userData.parts.gun) e.mesh.userData.parts.gun.rotation.z = Math.sin(e.bob * 0.45) * 0.07;
      if(e.mesh.userData.parts.cape) e.mesh.userData.parts.cape.rotation.x = 0.08 + Math.abs(Math.sin(e.bob * 0.7)) * 0.1;
      if(e.mesh.userData.parts.hatBadge) e.mesh.userData.parts.hatBadge.rotation.z = Math.sin(e.bob * 0.4) * 0.03;
    }

    const dx = player.pos.x - e.mesh.position.x;
    const dz = player.pos.z - e.mesh.position.z;
    const dist = Math.max(0.001, Math.hypot(dx, dz));
    const dirX = dx / dist;
    const dirZ = dz / dist;
    const sideX = -dirZ;
    const sideZ = dirX;

    const hpRatio = e.hp / e.maxHp;

    const preferRange =
      e.type === "tank" ? 8.5 :
      e.type === "elite" ? 10.5 :
      e.type === "runner" ? 4.8 : 7.0;

    let moveForward = 0;
    if(dist > preferRange + 0.8) moveForward = 1;
    else if(dist < preferRange - 0.8) moveForward = -0.75;

    let strafeStrength =
      e.type === "runner" ? 1.0 :
      e.type === "elite" ? 0.8 :
      e.type === "tank" ? 0.35 : 0.5;

    let strafeDir = Math.sin(e.aiClock * e.wobble + i) >= 0 ? 1 : -1;

    // Beschadigde vijanden worden agressiever / slimmer
    if(hpRatio < 0.45){
      strafeStrength += 0.18;
      if(e.type !== "tank") moveForward += 0.12;
    }

    // Runner kan charge doen
    let speedMul = 1;
    if(e.type === "runner" && e.chargeCooldown <= 0 && dist > 3.5 && dist < 10){
      moveForward = 1.45;
      strafeStrength = 0.18;
      speedMul = 1.55;
      e.chargeCooldown = rand(3.5, 5.5);
      createShockwave(e.mesh.position.clone(), 0xff6ea1, 1.6);
    }

    // Elite dodge / sidestep voor "slimmere" AI
    if((e.type === "elite" || e.type === "runner") && e.dodgeCooldown <= 0 && dist < 13){
      strafeDir *= -1;
      strafeStrength += 1.4;
      e.dodgeCooldown = e.type === "elite" ? rand(0.9, 1.7) : rand(1.2, 2.0);
    }

    // Tanks drukken juist meer door
    if(e.type === "tank"){
      moveForward += dist > 6.5 ? 0.2 : -0.05;
    }

    const wobble = Math.sin(now * (1.4 + e.wobble) + i * 1.7) * 0.16;
    const moveX = (dirX * moveForward + sideX * (strafeDir * strafeStrength + wobble) * 0.42) * e.speed * speedMul * dt;
    const moveZ = (dirZ * moveForward + sideZ * (strafeDir * strafeStrength + wobble) * 0.42) * e.speed * speedMul * dt;

    let nx = e.mesh.position.x + moveX;
    let nz = e.mesh.position.z + moveZ;

    // Botsingen met arena
    if(!collidesAt(nx, nz, e.radius)){
      e.mesh.position.x = nx;
      e.mesh.position.z = nz;
    } else {
      // probeer alsnog zijwaarts te glijden langs muren
      const slideX = e.mesh.position.x + sideX * strafeDir * e.speed * 0.8 * dt;
      const slideZ = e.mesh.position.z + sideZ * strafeDir * e.speed * 0.8 * dt;
      if(!collidesAt(slideX, slideZ, e.radius)){
        e.mesh.position.x = slideX;
        e.mesh.position.z = slideZ;
      }
    }

    // Scheiding tussen enemies zodat ze minder op elkaar klonteren
    for(let j=state.enemies.length-1;j>=0;j--){
      if(i === j) continue;
      const o = state.enemies[j];
      const sx = e.mesh.position.x - o.mesh.position.x;
      const sz = e.mesh.position.z - o.mesh.position.z;
      const dd = Math.hypot(sx, sz) || 0.001;
      const minDist = (e.radius + o.radius) * 0.72;
      if(dd < minDist){
        const push = (minDist - dd) * 0.045;
        e.mesh.position.x += (sx / dd) * push;
        e.mesh.position.z += (sz / dd) * push;
      }
    }

    e.mesh.position.y = 0.02 + Math.sin(e.bob) * (e.type === "runner" ? 0.05 : 0.04);

    if(e.groundRing){
      e.groundRing.position.set(e.mesh.position.x, 0.03, e.mesh.position.z);
      e.groundRing.rotation.z += dt * (e.type === "elite" ? 1.4 : e.type === "runner" ? 0.7 : 0.2);
      e.groundRing.material.opacity = 0.12 + 0.16 * (e.hp / e.maxHp);
    }

    e.mesh.lookAt(player.pos.x, 1.6, player.pos.z);

    // Melee pressure
    if(dist < (e.type === "tank" ? 2.2 : e.type === "elite" ? 1.9 : e.type === "runner" ? 1.45 : 1.6)){
      applyDamage((e.type === "tank" ? 20 : e.type === "elite" ? 18 : e.type === "runner" ? 14 : 13) * Math.min(2.1, waveRamp(player.wave) * 0.9) * dt * 8);
      if(Math.random() < dt * (e.type === "runner" ? 3.6 : 2.1)){
        createShockwave(
          e.mesh.position.clone(),
          e.type === "elite" ? 0xffa86e : 0xff6ea1,
          e.type === "tank" ? 2.6 : 1.7
        );
      }
    }

    // Fire behaviour per enemy type
    if(e.type === "elite" && e.fireCooldown <= 0 && dist < 15){
      if(e.burstCooldown <= 0){
        // elite burst / bombardement
        createShockwave(e.mesh.position.clone(), 0xffa86e, 3.8);
        for(let s=0;s<3;s++){
          setTimeout(() => {
            if(state.running && player.alive){
              explodeAt(player.pos.clone(), 2.8, 8, 0xffa86e);
            }
          }, s * 180);
        }
        e.burstCooldown = rand(3.4, 5.2);
        e.fireCooldown = rand(player.wave >= 12 ? 1.0 : 1.25, player.wave >= 12 ? 1.55 : 2.0);
      } else {
        enemyShoot(e);
        e.fireCooldown = rand(player.wave >= 12 ? 0.8 : 1.0, player.wave >= 12 ? 1.25 : 1.55);
      }
    } else if(e.type === "runner" && e.fireCooldown <= 0 && dist < 12){
      if(Math.random() < 0.45) enemyShoot(e);
      e.fireCooldown = rand(player.wave >= 12 ? 0.95 : 1.25, player.wave >= 12 ? 1.5 : 1.9);
    } else if(e.type === "tank" && e.fireCooldown <= 0 && dist < 18){
      enemyShoot(e);
      if(Math.random() < 0.35) enemyShoot(e);
      e.fireCooldown = rand(player.wave >= 12 ? 1.2 : 1.7, player.wave >= 12 ? 1.95 : 2.45);
    } else if(e.type === "basic" && e.fireCooldown <= 0 && dist < 22){
      enemyShoot(e);
      e.fireCooldown = rand(player.wave >= 12 ? 0.72 : 0.95, player.wave >= 12 ? 1.15 : 1.55);
    }
  }

  if(state.boss){
    const e = state.boss;

    if(e.phase == null) e.phase = 1;
    if(e.dashCooldown == null) e.dashCooldown = 4.5;
    if(e.volleyCooldown == null) e.volleyCooldown = 2.2;
    if(e.slamCooldown == null) e.slamCooldown = 6.2;

    e.bob += dt * 2.35;
    e.fireCooldown -= dt;
    e.dashCooldown -= dt;
    e.volleyCooldown -= dt;
    e.slamCooldown -= dt;

    const hpRatio = e.hp / e.maxHp;
    e.phase = hpRatio < 0.33 ? 3 : hpRatio < 0.66 ? 2 : 1;

    if(e.mesh.userData.parts){
      const swing = Math.sin(e.bob) * (e.phase === 3 ? 0.34 : 0.24);
      e.mesh.userData.parts.armL.rotation.x = swing * 0.55;
      e.mesh.userData.parts.armR.rotation.x = -swing * 0.55;
      e.mesh.userData.parts.legL.rotation.x = -swing * 0.8;
      e.mesh.userData.parts.legR.rotation.x = swing * 0.8;
      if(e.mesh.userData.parts.cape) e.mesh.userData.parts.cape.rotation.x = 0.1 + Math.abs(Math.sin(e.bob * 0.52)) * 0.13;
      if(e.mesh.userData.parts.hatBadge) e.mesh.userData.parts.hatBadge.rotation.z = Math.sin(e.bob * 0.35) * 0.05;
    }

    const dx = player.pos.x - e.mesh.position.x;
    const dz = player.pos.z - e.mesh.position.z;
    const dist = Math.max(0.001, Math.hypot(dx, dz));
    const dirX = dx / dist;
    const dirZ = dz / dist;
    const sideX = -dirZ;
    const sideZ = dirX;

    const enrage = Math.max(0, player.wave - 8) * 0.06;
    const targetRange = e.phase === 1 ? 11 : e.phase === 2 ? 9 : 7;

    let forward = 0;
    if(dist > targetRange + 1) forward = 1;
    else if(dist < targetRange - 1) forward = -0.6;

    const orbit = Math.sin(now * (e.phase === 3 ? 2.1 : 1.3)) * (e.phase === 3 ? 1.0 : 0.6);

    let nx = e.mesh.position.x + (dirX * forward + sideX * orbit * 0.45) * e.speed * dt;
    let nz = e.mesh.position.z + (dirZ * forward + sideZ * orbit * 0.45) * e.speed * dt;

    if(!collidesAt(nx, nz, e.radius)){
      e.mesh.position.x = nx;
      e.mesh.position.z = nz;
    }

    // Boss dash in fase 2/3
    if(e.phase >= 2 && e.dashCooldown <= 0 && dist > 5 && dist < 16){
      const dash = (e.phase === 3 ? 6.8 : 5.2) + Math.max(0, player.wave - 10) * 0.18;
      const tx = e.mesh.position.x + dirX * dash;
      const tz = e.mesh.position.z + dirZ * dash;
      if(!collidesAt(tx, tz, e.radius)){
        e.mesh.position.x = tx;
        e.mesh.position.z = tz;
        createShockwave(e.mesh.position.clone(), 0xff2e88, e.phase === 3 ? 5.2 : 4.3);
        if(dist < 8) applyDamage((e.phase === 3 ? 18 : 12) * (1 + enrage * 0.9));
      }
      e.dashCooldown = Math.max(1.8, (e.phase === 3 ? 3.2 : 4.4) - Math.min(1.4, enrage * 4.0));
    }

    e.mesh.position.y = 0.04 + Math.sin(e.bob) * 0.07;
    if(e.groundRing){
      e.groundRing.position.set(e.mesh.position.x, 0.03, e.mesh.position.z);
      e.groundRing.rotation.z += dt * (e.phase === 3 ? 2.5 : 1.2);
      e.groundRing.material.opacity = 0.18 + 0.22 * hpRatio;
    }

    e.mesh.lookAt(player.pos.x, 2.0, player.pos.z);

    if(dist < 2.7){
      applyDamage((e.phase === 3 ? 30 : 24) * (1 + enrage) * dt * 8);
    }

    // Boss slam
    if(e.slamCooldown <= 0 && dist < 10){
      createShockwave(e.mesh.position.clone(), 0xff6ea1, e.phase === 3 ? 6.0 : 5.0);
      explodeAt(player.pos.clone(), e.phase === 3 ? 4.2 : 3.3, (e.phase === 3 ? 17 : 12) * (1 + enrage * 0.75), 0xff6ea1);
      e.slamCooldown = Math.max(2.8, (e.phase === 3 ? 4.8 : 6.5) - Math.min(2.0, enrage * 5.0));
    }

    if(player.wave >= 9){
      if(e.reinforceCooldown == null) e.reinforceCooldown = Math.max(3.2, 7.0 - Math.min(2.5, Math.max(0, player.wave - 9) * 0.22));
      e.reinforceCooldown -= dt;
      if(e.reinforceCooldown <= 0 && state.enemies.length < Math.min(18, 6 + Math.floor(player.wave * 0.7))){
        const reinforcements = player.wave >= 15 ? 3 : player.wave >= 11 ? 2 : 1;
        showFloating(player.wave >= 12 ? "Boss calls reinforcements" : "Reinforcements incoming");
        for(let r=0;r<reinforcements;r++){
          setTimeout(() => {
            if(state.running && player.alive && state.boss === e) spawnEnemy(false);
          }, r * 140);
        }
        e.reinforceCooldown = Math.max(2.4, 6.4 - Math.min(3.1, Math.max(0, player.wave - 9) * 0.24));
      }
    }

    // Boss volley
    if(e.volleyCooldown <= 0 && dist < 30){
      const shots = (e.phase === 1 ? 2 : e.phase === 2 ? 3 : 5) + (player.wave >= 14 ? 1 : 0);
      for(let k=0;k<shots;k++){
        setTimeout(() => {
          if(state.running && player.alive && state.boss === e){
            enemyShoot(e);
          }
        }, k * Math.max(55, (e.phase === 3 ? 90 : 130) - Math.min(40, enrage * 110)));
      }
      e.volleyCooldown = Math.max(0.7, (e.phase === 3 ? 1.4 : e.phase === 2 ? 1.9 : 2.4) - Math.min(0.95, enrage * 2.2));
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
      updateHazards(dt);
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

  /* ===== CORE REWRITE — plak direct boven: ui.startBtn.addEventListener("click", startGame); ===== */

const CORE_LOOP = {
  lastDecorAt: 0,
  lastMinimapAt: 0,
  lastHudAt: 0,
  hudDirty: true,
  decorEveryMs: isTouch ? 50 : 33,
  minimapEveryMs: isTouch ? 125 : 83,
};

const CORE_TMP_SEARCH_DIR = new THREE.Vector3(0, -1, -1);

function coreMarkHudDirty(){
  CORE_LOOP.hudDirty = true;
}

function coreFlushHud(force = false){
  if(typeof updateHud !== "function") return;
  const now = performance.now();
  if(force || (CORE_LOOP.hudDirty && now - CORE_LOOP.lastHudAt >= 50)){
    CORE_LOOP.hudDirty = false;
    CORE_LOOP.lastHudAt = now;
    updateHud();
  }
}

function coreCleanupItem(item){
  if(!item) return;
  if(item.mesh) scene.remove(item.mesh);
  if(item.aura) scene.remove(item.aura);
  if(item.inner) scene.remove(item.inner);
  if(item.beam) scene.remove(item.beam);
  if(item.light) scene.remove(item.light);
  if(item.groundRing) scene.remove(item.groundRing);
}

function coreDrainArray(arr){
  while(arr.length){
    coreCleanupItem(arr.pop());
  }
}

function coreUpdateAmbientDecor(now, dt){
  if(stars) stars.rotation.y += dt * 0.01;
  if(skyline) skyline.position.x = Math.sin(now * 0.00006) * 1.6;

  if(arenaDeco?.crystalClusters?.children){
    arenaDeco.crystalClusters.children.forEach((crystal, i) => {
      crystal.position.y =
        crystal.userData.baseY +
        Math.sin(now * 0.0011 + crystal.userData.floatOffset + i) * 0.08;
      crystal.rotation.y += dt * 0.35;
    });
  }

  if(arenaDeco?.fogWisps?.children){
    arenaDeco.fogWisps.children.forEach((fog, i) => {
      fog.position.x =
        fog.userData.base.x +
        Math.sin(now * 0.00025 * (i + 1) + fog.userData.phase) * 3.2;
      fog.position.z =
        fog.userData.base.z +
        Math.cos(now * 0.0002 * (i + 1) + fog.userData.phase) * 2.4;
      fog.position.y =
        fog.userData.base.y +
        Math.sin(now * 0.0008 + fog.userData.phase) * 0.22;
    });
  }

  if(arenaDeco?.searchlights?.children){
    arenaDeco.searchlights.children.forEach((item, i) => {
      if(!item.userData.spot) return;
      item.rotation.y = Math.sin(now * 0.00045 + item.userData.phase) * 1.2;
      item.rotation.x = -0.26 + Math.sin(now * 0.00033 + item.userData.phase) * 0.12;

      const spot = item.userData.spot;
      CORE_TMP_SEARCH_DIR.set(0, -1, -1).applyEuler(item.rotation).normalize();
      spot.position.copy(item.position);
      spot.target.position.copy(item.position).add(CORE_TMP_SEARCH_DIR.multiplyScalar(36));
      spot.intensity = 1.15 + Math.sin(now * 0.0011 + i) * 0.18;
    });
  }

  if(arenaDeco?.skyBands?.children){
    arenaDeco.skyBands.children.forEach((band, i) => {
      band.position.y = band.userData.baseY + Math.sin(now * 0.00035 + band.userData.phase) * 1.6;
      band.position.x = Math.sin(now * 0.00022 + band.userData.phase) * 12;
      band.material.opacity = 0.05 + Math.sin(now * 0.0008 + band.userData.phase) * 0.03;
      band.rotation.z = Math.sin(now * 0.00012 + i) * 0.07;
    });
  }

  if(arenaDeco?.floatingShards?.children){
    arenaDeco.floatingShards.children.forEach((shard, i) => {
      shard.position.x = shard.userData.base.x + Math.sin(now * 0.0006 + shard.userData.phase + i) * 1.1;
      shard.position.y = shard.userData.base.y + Math.sin(now * 0.0012 + shard.userData.phase) * 0.48;
      shard.position.z = shard.userData.base.z + Math.cos(now * 0.00055 + shard.userData.phase) * 1.1;
      shard.rotation.x += dt * shard.userData.spin;
      shard.rotation.y += dt * shard.userData.spin * 0.8;
    });
  }

  if(arenaDeco?.monoliths?.children){
    arenaDeco.monoliths.children.forEach((monolith, i) => {
      monolith.material.emissiveIntensity = 0.08 + Math.sin(now * 0.0011 + i) * 0.05;
    });
  }

  if(emberField?.children){
    emberField.children.forEach((ember, i) => {
      ember.position.y = ember.userData.baseY + Math.sin(now * 0.001 * ember.userData.speed + i) * 0.18;
      ember.position.x += Math.sin(now * 0.0002 + i) * 0.0008;
    });
  }

  if(neonA){
    neonA.position.x = Math.sin(now * 0.00045) * 12;
    neonA.position.z = Math.cos(now * 0.00042) * 10;
  }

  if(neonB){
    neonB.position.x = Math.cos(now * 0.0005) * -12;
    neonB.position.z = Math.sin(now * 0.00047) * 10;
  }

  if(moonGlow){
    moonGlow.intensity = 0.7 + Math.sin(now * 0.00035) * 0.1;
  }
}

function applyDamage(amount){
  if(!player.alive) return;
  if(player.damageCooldown > 0) return;

  player.hp = Math.max(0, player.hp - amount);
  player.damageCooldown = 0.32;
  state.cameraShake = Math.min(1.8, state.cameraShake + 0.65);

  if(ui.damageFlash){
    ui.damageFlash.style.opacity = "1";
    setTimeout(() => {
      if(ui.damageFlash) ui.damageFlash.style.opacity = "0";
    }, 90);
  }

  sfxDamage?.();
  setStat?.();
  coreMarkHudDirty();

  if(player.hp <= 0){
    player.alive = false;
    state.running = false;
    submitScore?.();

    ui.center?.classList.remove("hidden");
    if(ui.center?.querySelector("h1")) ui.center.querySelector("h1").textContent = "Game over";
    if(ui.center?.querySelector("p")) ui.center.querySelector("p").textContent = "Je score is opgeslagen in de online leaderboard.";
    if(ui.startBtn) ui.startBtn.style.display = "none";
    if(ui.restartBtn) ui.restartBtn.style.display = "";

    if(document.pointerLockElement === renderer.domElement){
      document.exitPointerLock?.();
    }

    coreFlushHud(true);
  }
}

function killEnemy(enemy){
  if(!enemy) return;

  spawnRagdoll?.(enemy);
  scene.remove(enemy.mesh);
  if(enemy.groundRing) scene.remove(enemy.groundRing);

  createBurst?.(
    enemy.mesh.position.clone().add(new THREE.Vector3(0, 1.8, 0)),
    enemy.isBoss
      ? 0xff6ea1
      : enemy.type === "elite"
      ? 0xffa86e
      : enemy.type === "runner"
      ? 0x9dff7c
      : enemy.type === "tank"
      ? 0xffd166
      : 0x74a8ff,
    enemy.isBoss ? 28 : 16,
    enemy.isBoss ? 8 : 5
  );

  if(enemy.isBoss){
    registerKill(150);
    state.boss = null;
    ui.bossBarWrap?.classList.remove("show");
    dropPickup?.(enemy.mesh.position.clone());
    dropPickup?.(enemy.mesh.position.clone().add(new THREE.Vector3(1, 0, 0)));
    dropPickup?.(enemy.mesh.position.clone().add(new THREE.Vector3(-1, 0, 0)));
    sfxBoss?.();
    queueNextWave(1.1);
  } else {
    registerKill(
      enemy.type === "elite"
        ? 24
        : enemy.type === "tank"
        ? 18
        : enemy.type === "runner"
        ? 12
        : 10
    );
    dropPickup?.(enemy.mesh.position.clone());
    if(state.enemies.length <= 1 && !state.boss) queueNextWave(1.0);
  }

  state.lastClearStamp = performance.now();
  sfxEnemyDown?.();
  coreMarkHudDirty();
}

function restartGame(){
  coreDrainArray(state.bullets);
  coreDrainArray(state.enemyBullets);
  coreDrainArray(state.particles);
  coreDrainArray(state.pickups);
  coreDrainArray(state.rings);
  coreDrainArray(state.hazards);

  while(state.ragdolls.length){
    const rag = state.ragdolls.pop();
    if(rag?.pieces){
      for(const piece of rag.pieces){
        if(piece?.mesh) scene.remove(piece.mesh);
      }
    }
  }

  for(const e of state.enemies){
    if(e?.mesh) scene.remove(e.mesh);
    if(e?.groundRing) scene.remove(e.groundRing);
  }
  state.enemies.length = 0;

  if(state.boss){
    if(state.boss.mesh) scene.remove(state.boss.mesh);
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

  [ui.chipPlasma, ui.chipMine, ui.chipOrbital, ui.abilityPlasma, ui.abilityMine, ui.abilityOrbital]
    .forEach(el => el?.classList.remove("active", "fired"));

  state.combo = 1;
  state.comboTimer = 0;
  state.comboBest = 1;
  state.emergencyAmmoTimer = 0;
  state.ammoHintTimer = 0;
  state.fireHeld = false;
  state.nextWaveQueued = false;
  state.running = true;
  state.viewKick = 0;
  state.cameraShake = 0;
  state.lastClearStamp = performance.now();

  while(state.flashes.length){
    const flash = state.flashes.pop();
    if(flash?.light) scene.remove(flash.light);
  }

  restoreDefaultHint?.();
  setWeapon?.("bullet");

  state.songClock = audioCtx ? audioCtx.currentTime + 0.05 : 0;
  state.songStep = -1;

  lookYaw = 0;
  lookPitch = 0;
  input.lookX = 0;
  input.lookY = 0;
  applyCameraLook?.();

  ui.center?.classList.add("hidden");
  if(ui.startBtn) ui.startBtn.style.display = "";
  if(ui.restartBtn) ui.restartBtn.style.display = "none";
  ui.bossBarWrap?.classList.remove("show");

  setStat?.();
  coreMarkHudDirty();
  coreFlushHud(true);
  spawnWave?.();
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

function animate(now){
  requestAnimationFrame(animate);

  const dt = Math.min(0.033, (now - state.lastTime) / 1000 || 0.016);
  state.lastTime = now;

  if(now - CORE_LOOP.lastDecorAt >= CORE_LOOP.decorEveryMs){
    const decorDt = Math.min(0.05, (now - CORE_LOOP.lastDecorAt) / 1000 || dt);
    CORE_LOOP.lastDecorAt = now;
    coreUpdateAmbientDecor(now, decorDt);
  }

  if(state.running){
    updateMusic?.();
    updateTimers?.(dt);
    updateMovement?.(dt);
    updateBullets?.(dt);
    updateEnemies?.(dt);
    updateHazards?.(dt);
    updateParticles?.(dt);
    updateEffects?.(dt);
    updateRagdolls?.(dt);
    updatePickups?.(dt);
    tryAdvanceWave();

    if(state.fireHeld && !isTouch && player.weapon === "bullet"){
      shootWithDirection?.();
    }
  } else {
    updateViewWeapon?.(dt);
    updateEffects?.(dt);
    updateParticles?.(dt);
    updateRagdolls?.(dt);
  }

  if(now - CORE_LOOP.lastMinimapAt >= CORE_LOOP.minimapEveryMs){
    CORE_LOOP.lastMinimapAt = now;
    drawMinimap?.();
  }

  coreFlushHud();
  renderer.render(scene, camera);
}

function startGame(){
  ensureAudio?.();

  if(audioCtx && state.songClock < audioCtx.currentTime){
    state.songClock = audioCtx.currentTime + 0.05;
    state.songStep = -1;
  }

  if(collidesAt(player.pos.x, player.pos.z, player.radius)){
    resetPlayerPosition();
  }

  state.running = true;
  player.alive = true;
  ui.center?.classList.add("hidden");

  if(!state.enemies.length && !state.boss){
    spawnWave?.();
  }

  if(!isTouch){
    renderer.domElement.requestPointerLock?.();
  }

  coreMarkHudDirty();
  coreFlushHud(true);
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

/* =========================
   OLDE HANTER APOCALYPSE PACK
   plak dit direct boven: animate(performance.now());
   ========================= */
(() => {
  const apoc = {
    fury: 0,
    furyMax: 100,
    furyActive: false,
    furyTime: 0,
    dashCd: 0,
    lastExtraShot: 0,
    lastAuraPulse: 0,
    drones: [],
    overlay: null,
    hud: {},
    mobile: {},
    extraLast: performance.now()
  };

  function ohClamp(v, a, b){ return Math.max(a, Math.min(b, v)); }
  function ohLerp(a,b,t){ return a + (b-a) * t; }

  function addApocStyles(){
    const style = document.createElement("style");
    style.textContent = `
      #apocHud{
        position:fixed; right:16px; top:16px; z-index:25; width:min(320px,calc(100vw - 32px));
        display:flex; flex-direction:column; gap:10px; pointer-events:none;
      }
      .apoc-card{
        pointer-events:auto;
        background:linear-gradient(180deg, rgba(8,8,18,.78), rgba(14,8,28,.64));
        border:1px solid rgba(255,255,255,.14);
        border-radius:16px;
        padding:10px 12px;
        box-shadow:0 10px 32px rgba(0,0,0,.28), 0 0 24px rgba(157,107,255,.12);
        backdrop-filter: blur(12px) saturate(1.15);
      }
      .apoc-row{
        display:flex; align-items:center; justify-content:space-between; gap:10px;
        color:#fff; font-weight:800; letter-spacing:.03em;
      }
      .apoc-sub{ font-size:.8rem; color:rgba(255,255,255,.72); font-weight:700; }
      .apoc-meter{
        height:12px; margin-top:8px; border-radius:999px; overflow:hidden;
        border:1px solid rgba(255,255,255,.12);
        background:rgba(255,255,255,.08);
        box-shadow: inset 0 0 12px rgba(255,255,255,.04);
      }
      .apoc-meter > i{
        display:block; height:100%; width:0%;
        background:linear-gradient(90deg,#00f7ff,#8a2eff,#ff00a8,#ffe600,#00f7ff);
        background-size:220% 220%;
        animation:apocFlow 3.2s linear infinite;
        box-shadow:0 0 18px rgba(255,0,168,.22);
      }
      .apoc-ready{
        box-shadow:0 0 0 1px rgba(255,230,0,.22), 0 0 26px rgba(255,230,0,.16), 0 0 40px rgba(255,0,168,.12);
      }
      .apoc-btn{
        appearance:none; border:0; cursor:pointer;
        border-radius:999px; padding:.72rem 1rem; font-weight:900; color:#fff;
        background:linear-gradient(90deg,#ff00a8,#8a2eff,#00f7ff,#ffe600,#ff00a8);
        background-size:250% 250%;
        animation:apocFlow 4.5s linear infinite;
        box-shadow:0 0 18px rgba(255,0,168,.24), 0 0 30px rgba(0,247,255,.14);
        text-transform:uppercase; letter-spacing:.06em;
      }
      .apoc-btn:disabled{
        filter:grayscale(.6) brightness(.7);
        box-shadow:none; cursor:not-allowed;
      }
      #apocOverlay{
        position:fixed; inset:0; z-index:6; pointer-events:none; opacity:0;
        background:
          radial-gradient(circle at 50% 50%, rgba(255,255,255,.05), transparent 32%),
          radial-gradient(circle at 20% 30%, rgba(0,247,255,.12), transparent 28%),
          radial-gradient(circle at 80% 20%, rgba(255,0,168,.12), transparent 30%),
          radial-gradient(circle at 50% 80%, rgba(255,230,0,.10), transparent 30%);
        mix-blend-mode:screen;
        transition:opacity .18s ease;
      }
      #apocOverlay.active{
        opacity:1;
        animation:apocPulse .9s linear infinite;
      }
      #apocToast{
        position:fixed; left:50%; top:90px; transform:translateX(-50%);
        z-index:26; pointer-events:none;
        padding:.72rem 1rem; border-radius:999px;
        background:rgba(12,10,30,.74);
        border:1px solid rgba(255,255,255,.14);
        color:#fff; font-weight:900; letter-spacing:.06em;
        box-shadow:0 0 18px rgba(255,0,168,.16), 0 0 26px rgba(0,247,255,.10);
        opacity:0; transition:opacity .16s ease, transform .16s ease;
      }
      #apocToast.show{ opacity:1; transform:translateX(-50%) translateY(-4px); }

      #apocMobileDock{
        position:fixed; right:14px; bottom:124px; z-index:22;
        display:flex; flex-direction:column; gap:10px;
      }
      .apoc-mobile-btn{
        min-width:86px; min-height:58px;
        border-radius:18px; border:1px solid rgba(255,255,255,.16);
        background:linear-gradient(180deg, rgba(255,255,255,.14), rgba(255,255,255,.08));
        color:#fff; font-weight:900; letter-spacing:.04em;
        box-shadow:0 0 18px rgba(0,0,0,.18), inset 0 0 18px rgba(255,255,255,.03);
        backdrop-filter: blur(10px) saturate(1.08);
      }
      .apoc-mobile-btn small{
        display:block; font-size:.72rem; font-weight:700; opacity:.8; margin-top:2px;
      }
      .apoc-mobile-btn.ready{
        box-shadow:0 0 24px rgba(255,230,0,.22), 0 0 34px rgba(255,0,168,.14), inset 0 0 18px rgba(255,255,255,.04);
      }

      @keyframes apocFlow{
        0%{ background-position:0% 50%; }
        100%{ background-position:200% 50%; }
      }
      @keyframes apocPulse{
        0%{ filter:hue-rotate(0deg) saturate(1.0); }
        100%{ filter:hue-rotate(360deg) saturate(1.3); }
      }
      @media (max-width:780px){
        #apocHud{ right:10px; top:10px; width:min(280px,calc(100vw - 20px)); }
      }
    `;
    document.head.appendChild(style);
  }

  function makeHud(){
    addApocStyles();

    apoc.overlay = document.createElement("div");
    apoc.overlay.id = "apocOverlay";
    document.body.appendChild(apoc.overlay);

    const toast = document.createElement("div");
    toast.id = "apocToast";
    document.body.appendChild(toast);
    apoc.hud.toast = toast;

    const hud = document.createElement("div");
    hud.id = "apocHud";
    hud.innerHTML = `
      <div id="apocCard" class="apoc-card">
        <div class="apoc-row">
          <span>FURY MODE</span>
          <span id="apocFuryPct">0%</span>
        </div>
        <div class="apoc-sub" id="apocFuryText">Maak kills om Fury op te laden</div>
        <div class="apoc-meter"><i id="apocFuryFill"></i></div>
        <div style="display:flex;gap:8px;margin-top:10px;pointer-events:auto">
          <button id="apocFuryBtn" class="apoc-btn" type="button" disabled>Q / V Fury</button>
          <button id="apocDashBtn" class="apoc-btn" type="button">Shift Dash</button>
        </div>
      </div>
    `;
    document.body.appendChild(hud);

    apoc.hud.root = hud;
    apoc.hud.card = hud.querySelector("#apocCard");
    apoc.hud.fill = hud.querySelector("#apocFuryFill");
    apoc.hud.pct = hud.querySelector("#apocFuryPct");
    apoc.hud.text = hud.querySelector("#apocFuryText");
    apoc.hud.furyBtn = hud.querySelector("#apocFuryBtn");
    apoc.hud.dashBtn = hud.querySelector("#apocDashBtn");

    const mobileDock = document.createElement("div");
    mobileDock.id = "apocMobileDock";
    mobileDock.innerHTML = `
      <button id="apocMobileFury" class="apoc-mobile-btn" type="button">V FURY<small>combo mode</small></button>
      <button id="apocMobileDash" class="apoc-mobile-btn" type="button">SHIFT<small>dash</small></button>
    `;
    document.body.appendChild(mobileDock);

    apoc.mobile.root = mobileDock;
    apoc.mobile.fury = mobileDock.querySelector("#apocMobileFury");
    apoc.mobile.dash = mobileDock.querySelector("#apocMobileDash");

    if(!isTouch){
      mobileDock.style.display = "none";
    }

    apoc.hud.furyBtn.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio?.();
      if(!state.running) startGame?.();
      activateFury();
    });
    apoc.hud.dashBtn.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio?.();
      if(!state.running) startGame?.();
      doDash();
    });
    apoc.mobile.fury.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio?.();
      if(!state.running) startGame?.();
      activateFury();
    });
    apoc.mobile.dash.addEventListener("pointerdown", e => {
      e.preventDefault();
      e.stopPropagation();
      ensureAudio?.();
      if(!state.running) startGame?.();
      doDash();
    });

    updateApocHud();
  }

  function apocToast(msg){
    if(!apoc.hud.toast) return;
    apoc.hud.toast.textContent = msg;
    apoc.hud.toast.classList.add("show");
    clearTimeout(apoc.hud.toastTimer);
    apoc.hud.toastTimer = setTimeout(() => apoc.hud.toast.classList.remove("show"), 1200);
  }

  function updateApocHud(){
    if(!apoc.hud.fill) return;
    const pct = ohClamp((apoc.fury / apoc.furyMax) * 100, 0, 100);
    apoc.hud.fill.style.width = pct.toFixed(1) + "%";
    apoc.hud.pct.textContent = Math.round(pct) + "%";

    const ready = apoc.fury >= apoc.furyMax && !apoc.furyActive;
    apoc.hud.card.classList.toggle("apoc-ready", ready);
    apoc.hud.furyBtn.disabled = !ready && !apoc.furyActive;
    apoc.mobile.fury.classList.toggle("ready", ready);

    if(apoc.furyActive){
      apoc.hud.text.textContent = `FURY ACTIEF • ${apoc.furyTime.toFixed(1)}s • drones online`;
      apoc.hud.furyBtn.textContent = "Fury actief";
      apoc.mobile.fury.innerHTML = `FURY<small>${apoc.furyTime.toFixed(1)}s</small>`;
    }else if(ready){
      apoc.hud.text.textContent = "Vol! Druk op Q of V om los te gaan";
      apoc.hud.furyBtn.textContent = "Q / V Fury";
      apoc.mobile.fury.innerHTML = `V FURY<small>gereed</small>`;
    }else{
      apoc.hud.text.textContent = "Maak kills om Fury op te laden";
      apoc.hud.furyBtn.textContent = "Q / V Fury";
      apoc.mobile.fury.innerHTML = `V FURY<small>combo mode</small>`;
    }

    const dashReady = apoc.dashCd <= 0;
    apoc.hud.dashBtn.disabled = !dashReady;
    apoc.hud.dashBtn.textContent = dashReady ? "Shift Dash" : `Dash ${apoc.dashCd.toFixed(1)}s`;
    apoc.mobile.dash.classList.toggle("ready", dashReady);
    apoc.mobile.dash.innerHTML = dashReady ? `SHIFT<small>dash</small>` : `SHIFT<small>${apoc.dashCd.toFixed(1)}s</small>`;
  }

  function addFury(amount){
    if(apoc.furyActive) return;
    apoc.fury = ohClamp(apoc.fury + amount, 0, apoc.furyMax);
    updateApocHud();
    if(apoc.fury >= apoc.furyMax){
      flashHint?.("FURY VOL — druk op Q of V");
      apocToast("FURY READY");
    }
  }

  function makeDrone(color = 0x8bf0ff){
    const g = new THREE.Group();

    const core = new THREE.Mesh(
      new THREE.SphereGeometry(0.22, 14, 14),
      new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: 1.25,
        metalness: 0.32,
        roughness: 0.18
      })
    );
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(0.34, 0.04, 10, 22),
      new THREE.MeshBasicMaterial({ color, transparent:true, opacity:.8 })
    );
    ring.rotation.x = Math.PI / 2;

    const finA = new THREE.Mesh(
      new THREE.BoxGeometry(0.62, 0.05, 0.12),
      new THREE.MeshStandardMaterial({ color:0xffffff, emissive:color, emissiveIntensity:.5 })
    );
    const finB = finA.clone();
    finB.rotation.y = Math.PI / 2;

    g.add(core, ring, finA, finB);
    scene.add(g);

    return {
      mesh: g,
      ring,
      core,
      angle: Math.random() * Math.PI * 2,
      radius: 2.5 + Math.random() * 0.55,
      y: 1.8 + Math.random() * 0.25,
      fireCd: 0.1 + Math.random() * 0.2,
      bob: Math.random() * Math.PI * 2,
      spin: 1.2 + Math.random() * 0.8
    };
  }

  function spawnDrones(){
    clearDrones();
    apoc.drones.push(makeDrone(0x8bf0ff));
    apoc.drones.push(makeDrone(0xff7ce0));
  }

  function clearDrones(){
    while(apoc.drones.length){
      const d = apoc.drones.pop();
      scene.remove(d.mesh);
    }
  }

  function nearestEnemy(maxDist = 20){
    let best = null;
    let bestDist = maxDist;

    if(state.boss?.mesh){
      const dist = state.boss.mesh.position.distanceTo(player.pos);
      if(dist < bestDist){
        best = state.boss;
        bestDist = dist;
      }
    }

    for(const e of state.enemies){
      if(!e?.mesh) continue;
      const dist = e.mesh.position.distanceTo(player.pos);
      if(dist < bestDist){
        best = e;
        bestDist = dist;
      }
    }
    return best;
  }

  function damageEnemyDirect(enemy, damage){
    if(!enemy || !enemy.mesh) return false;
    enemy.hp -= damage;
    const hitPos = enemy.mesh.position.clone().add(new THREE.Vector3(0, 1.2, 0));
    createBurst?.(hitPos, 0xfff7bf, 5, 1.6, { minLife:.10, maxLife:.22, gravity:0.2, shrink:0.92 });
    createFlash?.(hitPos, 0xffffff, 1.2, 4.2, 0.07);

    if(enemy.hp <= 0){
      if(enemy === state.boss){
        killEnemy(enemy);
      }else{
        const idx = state.enemies.indexOf(enemy);
        if(idx !== -1){
          killEnemy(enemy);
          state.enemies.splice(idx, 1);
        }
      }
    }
    return true;
  }

  function droneShoot(drone, enemy){
    if(!enemy?.mesh) return;
    const from = drone.mesh.position.clone();
    const to = enemy.mesh.position.clone().add(new THREE.Vector3(0, 1.3, 0));
    const dir = to.clone().sub(from).normalize();

    state.bullets.push(createProjectile(from, dir, {
      speed: 28,
      friendly: true,
      color: 0x9cfbff,
      trailColor: 0xffffff,
      size: 0.09,
      life: 1.2,
      damage: 9,
      type: "drone"
    }));
    createFlash?.(from.clone(), 0x8bf0ff, 1.0, 2.5, 0.05);
  }

  function updateDrones(dt, now){
    if(!apoc.drones.length) return;

    apoc.drones.forEach((d, i) => {
      d.angle += dt * (1.4 + i * 0.25);
      d.bob += dt * 2.8;
      d.fireCd -= dt;
      const offX = Math.cos(d.angle) * d.radius;
      const offZ = Math.sin(d.angle) * d.radius;
      const y = d.y + Math.sin(d.bob) * 0.18;
      d.mesh.position.set(player.pos.x + offX, y, player.pos.z + offZ);
      d.mesh.rotation.y += dt * 4.8;
      d.ring.rotation.z += dt * d.spin;

      const enemy = nearestEnemy(19);
      if(apoc.furyActive && enemy && d.fireCd <= 0){
        d.fireCd = 0.22 + Math.random() * 0.08;
        droneShoot(d, enemy);
      }
    });
  }

  function activateFury(){
    if(apoc.furyActive || apoc.fury < apoc.furyMax) return;
    apoc.furyActive = true;
    apoc.fury = 0;
    apoc.furyTime = 12;
    apoc.lastExtraShot = 0;
    apoc.lastAuraPulse = 0;
    apoc.overlay.classList.add("active");

    spawnDrones();
    player.damageCooldown = Math.max(player.damageCooldown, 0.2);
    state.cameraShake = Math.min(2.0, state.cameraShake + 0.9);
    createShockwave?.(player.pos.clone(), 0xff00a8, 4.2);
    createShockwave?.(player.pos.clone(), 0x00f7ff, 5.4);
    flashHint?.("FURY MODE geactiveerd");
    apocToast("FURY MODE");
    updateApocHud();
  }

  function stopFury(silent=false){
    apoc.furyActive = false;
    apoc.furyTime = 0;
    apoc.overlay.classList.remove("active");
    clearDrones();
    if(!silent){
      flashHint?.("Fury voorbij");
    }
    updateApocHud();
  }

  function getMoveVector(){
    const v = new THREE.Vector3();
    if(input.forward || input.strafe){
      const forward = new THREE.Vector3(Math.sin(lookYaw), 0, Math.cos(lookYaw));
      const right = new THREE.Vector3(Math.cos(lookYaw), 0, -Math.sin(lookYaw));
      v.addScaledVector(forward, input.forward);
      v.addScaledVector(right, input.strafe);
    }else{
      camera.getWorldDirection(v);
      v.y = 0;
    }
    if(v.lengthSq() < 0.0001){
      v.set(Math.sin(lookYaw), 0, Math.cos(lookYaw));
    }
    return v.normalize();
  }

  function doDash(){
    if(!state.running || !player.alive || apoc.dashCd > 0) return;

    const dir = getMoveVector();
    const old = player.pos.clone();
    let moved = false;

    for(let step = 1; step <= 10; step++){
      const test = old.clone().add(dir.clone().multiplyScalar(step * 0.55));
      const blocked = (typeof collidesAt === "function") ? collidesAt(test.x, test.z, player.radius * 0.86) : false;
      if(blocked) break;
      player.pos.copy(test);
      moved = true;
    }

    if(!moved) return;

    apoc.dashCd = 3.5;
    player.damageCooldown = Math.max(player.damageCooldown, 0.35);
    state.cameraShake = Math.min(1.8, state.cameraShake + 0.42);

    for(let i=0;i<5;i++){
      const p = old.clone().lerp(player.pos, i / 4);
      createFlash?.(p.clone().add(new THREE.Vector3(0, 1.1, 0)), 0x8bf0ff, 1.3, 3.2, 0.06);
    }
    createShockwave?.(player.pos.clone(), 0x8bf0ff, 2.6);
    flashHint?.("Dash");
    updateApocHud();
  }

  function furySideShots(dirOverride=null){
    if(!apoc.furyActive || !state.running || !player.alive) return;
    if(player.weapon !== "bullet") return;

    const now = performance.now();
    if(now - apoc.lastExtraShot < 95) return;
    apoc.lastExtraShot = now;

    const dir = dirOverride ? dirOverride.clone().normalize() : new THREE.Vector3();
    if(!dirOverride){
      camera.getWorldDirection(dir);
      dir.normalize();
    }

    const base = player.pos.clone();
    base.y = 1.52;
    const right = new THREE.Vector3(dir.z, 0, -dir.x).normalize();
    const spreadA = dir.clone().addScaledVector(right, 0.12).normalize();
    const spreadB = dir.clone().addScaledVector(right, -0.12).normalize();

    [spreadA, spreadB].forEach((d, idx) => {
      const start = base.clone().addScaledVector(right, idx === 0 ? 0.24 : -0.24).add(d.clone().multiplyScalar(.8));
      state.bullets.push(createProjectile(start, d, {
        speed: 34,
        friendly: true,
        color: idx === 0 ? 0x00f7ff : 0xff7ce0,
        trailColor: 0xffffff,
        size: 0.08,
        life: 1.25,
        damage: 7,
        type: "fury"
      }));
    });

    createFlash?.(base.clone(), 0xffffff, 0.8, 2.0, 0.04);
  }

  function updateTemporaryRelicEffects(dt){
    if(player.speedBoostTimer > 0){
      player.speedBoostTimer = Math.max(0, player.speedBoostTimer - dt);
      if(player.speedBoostTimer <= 0){
        player.speedBoost = 0;
      }
    }
  }

  function updateFury(dt){
    if(apoc.dashCd > 0){
      apoc.dashCd = Math.max(0, apoc.dashCd - dt);
    }

    if(apoc.furyActive){
      apoc.furyTime = Math.max(0, apoc.furyTime - dt);

      if(player.weapon === "bullet"){
        player.fireCooldown *= 0.82;
      }else if(player.weapon === "rocket"){
        player.fireCooldown *= 0.9;
      }else if(player.weapon === "grenade"){
        player.fireCooldown *= 0.92;
      }

      apoc.overlay.style.opacity = String(0.52 + Math.sin(performance.now() * 0.012) * 0.12);

      apoc.lastAuraPulse -= dt;
      if(apoc.lastAuraPulse <= 0){
        apoc.lastAuraPulse = 0.48;
        createShockwave?.(player.pos.clone(), Math.random() > 0.5 ? 0xff00a8 : 0x00f7ff, 1.8 + Math.random() * 1.4);

        for(const e of state.enemies){
          if(!e?.mesh) continue;
          const dist = e.mesh.position.distanceTo(player.pos);
          if(dist < 4.4){
            damageEnemyDirect(e, 3);
          }
        }
        if(state.boss?.mesh && state.boss.mesh.position.distanceTo(player.pos) < 4.8){
          damageEnemyDirect(state.boss, 3);
        }
      }

      if(apoc.furyTime <= 0){
        stopFury();
      }
    }else{
      apoc.overlay.style.opacity = "0";
    }

    updateApocHud();
  }

  /* wrappers rond bestaande gamefuncties */
  const _registerKill = registerKill;
  registerKill = function(points){
    _registerKill(points);
    addFury(10 + state.combo * 4);

    if(apoc.furyActive){
      player.hp = Math.min(player.maxHp, player.hp + 4);
      setStat?.();
      createFlash?.(player.pos.clone().add(new THREE.Vector3(0,1.3,0)), 0x9dff7c, 1.2, 3.0, 0.06);
    }
  };

  const _restartGame = restartGame;
  restartGame = function(){
    stopFury(true);
    apoc.fury = 0;
    apoc.dashCd = 0;
    updateApocHud();
    return _restartGame();
  };

  const _shootWithDirection = shootWithDirection;
  shootWithDirection = function(dirOverride=null){
    const ok = _shootWithDirection(dirOverride);
    if(ok && apoc.furyActive){
      furySideShots(dirOverride);
    }
    return ok;
  };

  const _applyDamage = applyDamage;
  applyDamage = function(amount){
    if(apoc.furyActive){
      amount *= 0.78;
    }
    return _applyDamage(amount);
  };

  /* extra inputlaag: lost 4/5/6 issues op + nieuwe controls */
  function handleApocHotkeys(e){
    const code = e.code || "";
    const key = (e.key || "").toLowerCase();

    const hit = (wantedCodes, wantedKeys=[]) =>
      wantedCodes.includes(code) || wantedKeys.includes(key);

    if(hit(["Digit4","Numpad4"],["4","z"])){
      e.preventDefault();
      ensureAudio?.();
      if(!state.running) startGame?.();
      firePlasmaBurst?.();
      return;
    }
    if(hit(["Digit5","Numpad5"],["5","x"])){
      e.preventDefault();
      ensureAudio?.();
      if(!state.running) startGame?.();
      deployShockMine?.();
      return;
    }
    if(hit(["Digit6","Numpad6"],["6","c"])){
      e.preventDefault();
      ensureAudio?.();
      if(!state.running) startGame?.();
      deployOrbital?.();
      return;
    }
    if(hit(["KeyQ","KeyV"],["q","v"])){
      e.preventDefault();
      ensureAudio?.();
      if(!state.running) startGame?.();
      activateFury();
      return;
    }
    if(hit(["ShiftLeft","ShiftRight"],["shift"])){
      e.preventDefault();
      ensureAudio?.();
      if(!state.running) startGame?.();
      doDash();
    }
  }

  window.addEventListener("keydown", handleApocHotkeys, { capture:true, passive:false });

  /* frame wrapper */
  const _animate = animate;
  animate = function(now){
    const dt = Math.min(0.033, (now - apoc.extraLast) / 1000 || 0.016);
    apoc.extraLast = now;

    updateTemporaryRelicEffects(dt);
    updateFury(dt);
    updateDrones(dt, now);

    _animate(now);
  };

  makeHud();
})();

/* =========================
   OLDE HANTER NEMESIS PACK
   plak dit boven: animate(performance.now());
   werkt samen met de vorige expansion pack
   ========================= */
(() => {
  const nemesis = {
    lastNow: performance.now(),
    phase: 0,
    activeBossId: 0,
    activeEvent: null,
    eventTimer: 0,
    crateCooldown: 18,
    fogAlpha: 0,
    moonIntensity: 0,
    stormPulse: 0,
    bossMark: null,
    hud: {},
    bossDecor: [],
    pendingEventToast: 0
  };

  function nClamp(v, a, b){ return Math.max(a, Math.min(b, v)); }
  function nRand(a, b){ return a + Math.random() * (b - a); }

  function addNemesisStyles(){
    const style = document.createElement("style");
    style.textContent = `
      #nemesisHud{
        position:fixed; left:16px; top:16px; z-index:24;
        width:min(320px, calc(100vw - 32px));
        display:flex; flex-direction:column; gap:10px; pointer-events:none;
      }
      .nem-card{
        pointer-events:auto;
        padding:12px 14px;
        border-radius:18px;
        background:linear-gradient(180deg, rgba(12,12,22,.74), rgba(18,8,28,.60));
        border:1px solid rgba(255,255,255,.12);
        box-shadow:0 12px 36px rgba(0,0,0,.22), 0 0 22px rgba(255,110,161,.08);
        color:#fff;
        backdrop-filter: blur(12px) saturate(1.1);
      }
      .nem-title{
        font-size:.8rem; letter-spacing:.14em; text-transform:uppercase;
        opacity:.78; font-weight:900;
      }
      .nem-main{
        margin-top:6px;
        display:flex; justify-content:space-between; gap:12px; align-items:center;
        font-weight:900; font-size:1rem;
      }
      .nem-sub{
        margin-top:8px; color:rgba(255,255,255,.78); font-size:.82rem; font-weight:700;
      }
      .nem-bar{
        margin-top:10px; height:10px; border-radius:999px; overflow:hidden;
        background:rgba(255,255,255,.08);
        border:1px solid rgba(255,255,255,.10);
      }
      .nem-bar > i{
        display:block; height:100%; width:0%;
        background:linear-gradient(90deg, #ffe066, #ff6ea1, #8bf0ff, #ffe066);
        background-size:200% 200%;
        animation:nemflow 4s linear infinite;
      }
      #nemesisOverlayFog,
      #nemesisOverlayMoon,
      #nemesisOverlayStorm{
        position:fixed; inset:0; pointer-events:none; z-index:5;
        opacity:0; transition:opacity .35s ease;
      }
      #nemesisOverlayFog{
        background:
          radial-gradient(circle at 50% 60%, rgba(255,255,255,.08), transparent 28%),
          radial-gradient(circle at 20% 30%, rgba(180,220,255,.08), transparent 30%),
          radial-gradient(circle at 80% 70%, rgba(255,180,220,.08), transparent 34%),
          linear-gradient(180deg, rgba(210,220,255,.08), rgba(110,120,150,.14));
      }
      #nemesisOverlayMoon{
        background:
          radial-gradient(circle at 50% 18%, rgba(255,70,110,.20), transparent 16%),
          linear-gradient(180deg, rgba(80,0,18,.08), rgba(25,0,10,.18));
        mix-blend-mode:screen;
      }
      #nemesisOverlayStorm{
        background:
          linear-gradient(115deg, transparent 20%, rgba(139,240,255,.16) 42%, transparent 60%),
          linear-gradient(295deg, transparent 26%, rgba(255,255,255,.12) 46%, transparent 62%);
        background-size:220% 220%;
        animation:nemstorm 2.3s linear infinite;
      }
      @keyframes nemflow{
        0%{ background-position:0% 50%; }
        100%{ background-position:200% 50%; }
      }
      @keyframes nemstorm{
        0%{ background-position:0% 0%, 100% 100%; }
        100%{ background-position:200% 0%, -100% 100%; }
      }
      @media (max-width:780px){
        #nemesisHud{ left:10px; top:10px; width:min(280px, calc(100vw - 20px)); }
      }
    `;
    document.head.appendChild(style);
  }

  function buildNemesisHud(){
    addNemesisStyles();

    const fog = document.createElement("div");
    fog.id = "nemesisOverlayFog";
    document.body.appendChild(fog);

    const moon = document.createElement("div");
    moon.id = "nemesisOverlayMoon";
    document.body.appendChild(moon);

    const storm = document.createElement("div");
    storm.id = "nemesisOverlayStorm";
    document.body.appendChild(storm);

    const hud = document.createElement("div");
    hud.id = "nemesisHud";
    hud.innerHTML = `
      <div class="nem-card">
        <div class="nem-title">Arena Event</div>
        <div class="nem-main">
          <span id="nemEventName">Geen event</span>
          <span id="nemEventTime">0.0s</span>
        </div>
        <div class="nem-sub" id="nemEventDesc">De arena is stabiel.</div>
        <div class="nem-bar"><i id="nemEventFill"></i></div>
      </div>
      <div class="nem-card">
        <div class="nem-title">Boss Phase</div>
        <div class="nem-main">
          <span id="nemBossState">Geen baas</span>
          <span id="nemBossPhase">-</span>
        </div>
        <div class="nem-sub" id="nemBossDesc">Nog geen Nemesis actief.</div>
        <div class="nem-bar"><i id="nemBossFill"></i></div>
      </div>
    `;
    document.body.appendChild(hud);

    nemesis.hud.root = hud;
    nemesis.hud.fog = fog;
    nemesis.hud.moon = moon;
    nemesis.hud.storm = storm;
    nemesis.hud.eventName = hud.querySelector("#nemEventName");
    nemesis.hud.eventTime = hud.querySelector("#nemEventTime");
    nemesis.hud.eventDesc = hud.querySelector("#nemEventDesc");
    nemesis.hud.eventFill = hud.querySelector("#nemEventFill");
    nemesis.hud.bossState = hud.querySelector("#nemBossState");
    nemesis.hud.bossPhase = hud.querySelector("#nemBossPhase");
    nemesis.hud.bossDesc = hud.querySelector("#nemBossDesc");
    nemesis.hud.bossFill = hud.querySelector("#nemBossFill");

    refreshNemesisHud();
  }

  function refreshNemesisHud(){
    if(!nemesis.hud.root) return;

    if(nemesis.activeEvent){
      const max = nemesis.activeEvent.maxTime || 1;
      const pct = nClamp((nemesis.eventTimer / max) * 100, 0, 100);
      nemesis.hud.eventName.textContent = nemesis.activeEvent.label;
      nemesis.hud.eventTime.textContent = nemesis.eventTimer.toFixed(1) + "s";
      nemesis.hud.eventDesc.textContent = nemesis.activeEvent.desc;
      nemesis.hud.eventFill.style.width = pct.toFixed(1) + "%";
    }else{
      nemesis.hud.eventName.textContent = "Geen event";
      nemesis.hud.eventTime.textContent = "0.0s";
      nemesis.hud.eventDesc.textContent = "De arena is stabiel.";
      nemesis.hud.eventFill.style.width = "0%";
    }

    if(state.boss){
      const hpPct = nClamp(state.boss.hp / state.boss.maxHp, 0, 1);
      nemesis.hud.bossState.textContent = "Nemesis actief";
      nemesis.hud.bossPhase.textContent = "Fase " + (nemesis.phase + 1);
      nemesis.hud.bossDesc.textContent = bossPhaseText();
      nemesis.hud.bossFill.style.width = (hpPct * 100).toFixed(1) + "%";
    }else{
      nemesis.hud.bossState.textContent = "Geen baas";
      nemesis.hud.bossPhase.textContent = "-";
      nemesis.hud.bossDesc.textContent = "Nog geen Nemesis actief.";
      nemesis.hud.bossFill.style.width = "0%";
    }
  }

  function bossPhaseText(){
    if(!state.boss) return "Nog geen Nemesis actief.";
    if(nemesis.phase === 0) return "Zoekt je op en vuurt standaard salvo's.";
    if(nemesis.phase === 1) return "Wordt sneller en roept extra troepen op.";
    return "Wordt gevaarlijk: arena pulses, extra salvo's en hogere druk.";
  }

  function chooseArenaEvent(){
    const roll = Math.random();
    if(roll < 0.25){
      return {
        type: "fog",
        label: "Ghost Mist",
        desc: "Mist verlaagt zicht, vijanden komen dichterbij.",
        maxTime: 18,
        onStart(){
          flashHint?.("ARENA EVENT: GHOST MIST");
        },
        onUpdate(dt){
          nemesis.fogAlpha = nClamp(nemesis.fogAlpha + dt * 0.18, 0, 0.92);
          if(nemesis.hud.fog) nemesis.hud.fog.style.opacity = String(nemesis.fogAlpha * 0.85);
        },
        onEnd(){
          nemesis.fogAlpha = 0;
          if(nemesis.hud.fog) nemesis.hud.fog.style.opacity = "0";
        }
      };
    }
    if(roll < 0.5){
      return {
        type: "moon",
        label: "Blood Moon",
        desc: "Meer drops, maar vijanden bewegen sneller.",
        maxTime: 16,
        onStart(){
          flashHint?.("ARENA EVENT: BLOOD MOON");
        },
        onUpdate(dt){
          nemesis.moonIntensity = nClamp(nemesis.moonIntensity + dt * 0.22, 0, 1);
          if(nemesis.hud.moon) nemesis.hud.moon.style.opacity = String(0.7 * nemesis.moonIntensity);
          for(const e of state.enemies){
            if(e && !e.isBoss) e.speed *= 1.0009;
          }
        },
        onEnd(){
          nemesis.moonIntensity = 0;
          if(nemesis.hud.moon) nemesis.hud.moon.style.opacity = "0";
        }
      };
    }
    if(roll < 0.75){
      return {
        type: "storm",
        label: "Neon Storm",
        desc: "Bliksempulsen raken de arena en tikken vijanden weg.",
        maxTime: 14,
        pulse: 1.1,
        onStart(){
          flashHint?.("ARENA EVENT: NEON STORM");
        },
        onUpdate(dt){
          nemesis.stormPulse -= dt;
          if(nemesis.hud.storm) nemesis.hud.storm.style.opacity = "0.56";
          if(nemesis.stormPulse <= 0){
            nemesis.stormPulse = 1.1 + Math.random() * 0.55;
            doStormStrike();
          }
        },
        onEnd(){
          if(nemesis.hud.storm) nemesis.hud.storm.style.opacity = "0";
        }
      };
    }
    return {
      type: "rage",
      label: "Rage Protocol",
      desc: "Jij schiet sneller, maar Olde Hanter spawnt agressiever.",
      maxTime: 15,
      onStart(){
        flashHint?.("ARENA EVENT: RAGE PROTOCOL");
      },
      onUpdate(dt){
        player.fireCooldown *= 0.9;
        if(Math.random() < dt * 0.18 && state.enemies.length < 26 && state.running && player.alive){
          spawnEnemy(false);
        }
      },
      onEnd(){}
    };
  }

  function startArenaEvent(force=false){
    if(!state.running || !player.alive) return;
    if(nemesis.activeEvent && !force) return;

    if(nemesis.activeEvent?.onEnd) nemesis.activeEvent.onEnd();

    nemesis.activeEvent = chooseArenaEvent();
    nemesis.eventTimer = nemesis.activeEvent.maxTime;
    nemesis.pendingEventToast = 1.6;
    nemesis.activeEvent.onStart?.();
    refreshNemesisHud();
  }

  function endArenaEvent(){
    if(!nemesis.activeEvent) return;
    nemesis.activeEvent.onEnd?.();
    nemesis.activeEvent = null;
    nemesis.eventTimer = 0;
    refreshNemesisHud();
  }

  function doStormStrike(){
    const targets = [];
    if(state.boss?.mesh) targets.push(state.boss);
    for(const e of state.enemies){
      if(e?.mesh) targets.push(e);
    }
    if(!targets.length) return;
    const target = targets[(Math.random() * targets.length) | 0];
    const p = target.mesh.position.clone();
    createShockwave?.(p.clone(), 0x8bf0ff, 2.8);
    createFlash?.(p.clone().add(new THREE.Vector3(0, 2.5, 0)), 0xffffff, 1.4, 6.2, 0.07);
    createBurst?.(p.clone().add(new THREE.Vector3(0, 1.2, 0)), 0xdafcff, 10, 5, {
      minLife: .14, maxLife: .36, gravity: 0.5, shrink: 0.95
    });
    target.hp -= 18;
    if(target.hp <= 0){
      if(target === state.boss){
        killEnemy(target);
      }else{
        const idx = state.enemies.indexOf(target);
        if(idx !== -1){
          killEnemy(target);
          state.enemies.splice(idx, 1);
        }
      }
    }
  }

  function spawnSupplyCrate(pos){
    const group = new THREE.Group();

    const body = new THREE.Mesh(
      new THREE.BoxGeometry(0.9, 0.9, 0.9),
      new THREE.MeshStandardMaterial({
        color: 0x1d2130,
        emissive: 0x6d2a48,
        emissiveIntensity: 0.85,
        metalness: 0.34,
        roughness: 0.32
      })
    );
    const trim = new THREE.Mesh(
      new THREE.TorusGeometry(0.48, 0.06, 10, 24),
      new THREE.MeshStandardMaterial({
        color: 0xffd166,
        emissive: 0xffd166,
        emissiveIntensity: 0.85
      })
    );
    trim.rotation.x = Math.PI / 2;
    group.add(body, trim);

    group.position.copy(pos);
    group.position.y = 0.52;
    scene.add(group);

    const aura = new THREE.Mesh(
      new THREE.RingGeometry(0.8, 1.25, 34),
      new THREE.MeshBasicMaterial({
        color: 0xffd166,
        transparent: true,
        opacity: 0.55,
        side: THREE.DoubleSide
      })
    );
    aura.rotation.x = -Math.PI / 2;
    aura.position.set(pos.x, 0.03, pos.z);
    scene.add(aura);

    state.pickups.push({
      kind: "nemesisCrate",
      mesh: group,
      aura,
      life: 18,
      amount: 1,
      pulse: 0
    });
  }

  function spawnBossDecor(){
    clearBossDecor();
    if(!state.boss?.mesh) return;

    for(let i=0;i<3;i++){
      const ring = new THREE.Mesh(
        new THREE.TorusGeometry(2.8 + i * 0.55, 0.04 + i * 0.01, 8, 48),
        new THREE.MeshBasicMaterial({
          color: i === 1 ? 0x8bf0ff : 0xff6ea1,
          transparent: true,
          opacity: 0.26 - i * 0.05
        })
      );
      ring.rotation.x = Math.PI / 2;
      scene.add(ring);
      nemesis.bossDecor.push(ring);
    }
  }

  function clearBossDecor(){
    while(nemesis.bossDecor.length){
      scene.remove(nemesis.bossDecor.pop());
    }
  }

  function updateBossDecor(dt, now){
    if(!state.boss?.mesh){
      clearBossDecor();
      return;
    }
    if(!nemesis.bossDecor.length) spawnBossDecor();

    nemesis.bossDecor.forEach((ring, i) => {
      ring.position.copy(state.boss.mesh.position);
      ring.position.y = 0.15 + i * 0.28 + Math.sin(now * 0.002 + i) * 0.06;
      ring.rotation.z += dt * (0.55 + i * 0.22);
      ring.material.opacity = 0.16 + Math.sin(now * 0.003 + i) * 0.08;
    });
  }

  function updateBossPhases(dt){
    if(!state.boss?.mesh){
      nemesis.phase = 0;
      clearBossDecor();
      refreshNemesisHud();
      return;
    }

    const hpPct = state.boss.hp / state.boss.maxHp;
    const newPhase = hpPct < 0.33 ? 2 : hpPct < 0.66 ? 1 : 0;

    if(newPhase !== nemesis.phase){
      nemesis.phase = newPhase;
      state.cameraShake = Math.min(2.0, state.cameraShake + 0.8);
      flashHint?.(`BOSS PHASE ${nemesis.phase + 1}`);
      createShockwave?.(state.boss.mesh.position.clone(), nemesis.phase === 2 ? 0xffd166 : 0xff6ea1, 4 + nemesis.phase);
      if(nemesis.phase >= 1){
        for(let i=0;i<Math.min(2 + nemesis.phase, 4); i++){
          if(state.enemies.length < 28) spawnEnemy(false);
        }
      }
      if(nemesis.phase === 2){
        spawnSupplyCrate(player.pos.clone().add(new THREE.Vector3(nRand(-4,4),0,nRand(-4,4))));
      }
    }

    if(nemesis.phase >= 1){
      state.boss.speed *= 1.0007;
      state.boss.fireCooldown -= dt * 0.18;
    }
    if(nemesis.phase >= 2){
      state.boss.fireCooldown -= dt * 0.22;

      if(Math.random() < dt * 0.7){
        const pos = state.boss.mesh.position.clone().add(new THREE.Vector3(nRand(-2.8,2.8), 0, nRand(-2.8,2.8)));
        createShockwave?.(pos.clone(), 0xff6ea1, 1.6);
        createBurst?.(pos.clone().add(new THREE.Vector3(0,1,0)), 0xffb2c8, 6, 4.2, {
          minLife:.16, maxLife:.3, gravity:0.45, shrink:0.95
        });
        if(pos.distanceTo(player.pos) < 2.8){
          applyDamage?.(8);
        }
      }
    }

    refreshNemesisHud();
  }

  function updateArenaEvent(dt){
    if(!state.running || !player.alive){
      endArenaEvent();
      return;
    }

    nemesis.crateCooldown -= dt;
    if(nemesis.crateCooldown <= 0){
      nemesis.crateCooldown = 20 + Math.random() * 10;
      spawnSupplyCrate(new THREE.Vector3(nRand(-30,30), 0, nRand(-30,30)));
      flashHint?.("Supply crate gedropt");
    }

    if(!nemesis.activeEvent){
      if(player.wave >= 3 && Math.random() < dt * 0.04){
        startArenaEvent();
      }
      refreshNemesisHud();
      return;
    }

    nemesis.eventTimer = Math.max(0, nemesis.eventTimer - dt);
    nemesis.activeEvent.onUpdate?.(dt);

    if(nemesis.eventTimer <= 0){
      endArenaEvent();
    }

    refreshNemesisHud();
  }

  function updateCrates(dt, now){
    for(let i = state.pickups.length - 1; i >= 0; i--){
      const p = state.pickups[i];
      if(p.kind !== "nemesisCrate") continue;

      p.life -= dt;
      p.pulse = (p.pulse || 0) + dt * 3.2;

      if(p.mesh){
        p.mesh.rotation.y += dt * 1.25;
        p.mesh.position.y = 0.56 + Math.sin(now * 0.003 + i) * 0.08;
      }
      if(p.aura){
        p.aura.material.opacity = 0.25 + Math.sin(p.pulse) * 0.18;
      }

      if(player.pos.distanceTo(p.mesh.position) < 1.6){
        rewardFromCrate(p.mesh.position.clone());
        if(p.mesh) scene.remove(p.mesh);
        if(p.aura) scene.remove(p.aura);
        state.pickups.splice(i, 1);
        continue;
      }

      if(p.life <= 0){
        if(p.mesh) scene.remove(p.mesh);
        if(p.aura) scene.remove(p.aura);
        state.pickups.splice(i, 1);
      }
    }
  }

  function rewardFromCrate(pos){
    const roll = Math.random();
    if(roll < 0.2){
      player.hp = Math.min(player.maxHp, player.hp + 35);
      flashHint?.("Crate: HP boost");
    }else if(roll < 0.4){
      player.ammo.bullet += 50;
      player.ammo.rocket += 2;
      player.ammo.grenade += 1;
      flashHint?.("Crate: ammo cache");
    }else if(roll < 0.6){
      player.abilities.plasma += 2;
      player.abilities.mine += 1;
      player.abilities.orbital += 1;
      flashHint?.("Crate: skill refill");
    }else if(roll < 0.8){
      registerKill?.(40);
      flashHint?.("Crate: bonus score");
    }else{
      for(let i=0;i<3;i++){
        if(state.enemies.length < 28) spawnEnemy(false);
      }
      flashHint?.("Crate cursed: extra Olde Hanters!");
    }

    createShockwave?.(pos.clone(), 0xffd166, 3.2);
    createBurst?.(pos.clone().add(new THREE.Vector3(0,1,0)), 0xffe7a6, 16, 6.2, {
      minLife:.18, maxLife:.44, gravity:0.5, shrink:0.95
    });
    setStat?.();
  }

  const _spawnWave = spawnWave;
  spawnWave = function(){
    _spawnWave();

    if(player.wave >= 2){
      const bonus = Math.min(1 + Math.floor(player.wave / 5), 4);
      for(let i=0;i<bonus;i++){
        if(state.enemies.length < 30 && Math.random() < 0.55){
          spawnEnemy(false);
        }
      }
    }

    if(player.wave >= 4 && player.wave % 2 === 0){
      startArenaEvent(true);
    }

    if(player.wave % 4 === 0){
      flashHint?.("Nemesis voorwaarden bereikt");
    }

    refreshNemesisHud();
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    const wasBoss = !!enemy?.isBoss;
    const bossPos = enemy?.mesh?.position?.clone?.() || null;

    _killEnemy(enemy);

    if(wasBoss && bossPos){
      clearBossDecor();
      endArenaEvent();
      for(let i=0;i<2;i++){
        spawnSupplyCrate(bossPos.clone().add(new THREE.Vector3(i ? 2 : -2, 0, 0)));
      }
      registerKill?.(60);
      flashHint?.("Nemesis verslagen — loot gedropt");
    }else if(!wasBoss && Math.random() < 0.08){
      spawnSupplyCrate(enemy.mesh.position.clone());
    }

    refreshNemesisHud();
  };

  const _restartGame = restartGame;
  restartGame = function(){
    clearBossDecor();
    endArenaEvent();
    nemesis.phase = 0;
    nemesis.crateCooldown = 18;
    refreshNemesisHud();
    return _restartGame();
  };

  const _applyDamage = applyDamage;
  applyDamage = function(amount){
    if(nemesis.activeEvent?.type === "fog"){
      amount *= 1.08;
    }
    if(nemesis.activeEvent?.type === "rage"){
      amount *= 1.04;
    }
    return _applyDamage(amount);
  };

  const _animate = animate;
  animate = function(now){
    const dt = Math.min(0.033, (now - nemesis.lastNow) / 1000 || 0.016);
    nemesis.lastNow = now;

    updateArenaEvent(dt);
    updateBossPhases(dt);
    updateBossDecor(dt, now);
    updateCrates(dt, now);

    _animate(now);
  };

  buildNemesisHud();
})();

/* =========================
   OLDE HANTER HUD REBUILD PACK
   plak boven: animate(performance.now());
   ========================= */
(() => {
  function injectHudRebuildStyles(){
    const style = document.createElement("style");
    style.id = "ohHudRebuildStyles";
    style.textContent = `
      :root{
        --hud-top: calc(12px + env(safe-area-inset-top));
        --hud-bottom: calc(12px + env(safe-area-inset-bottom));
        --hud-left: max(12px, env(safe-area-inset-left));
        --hud-right: max(12px, env(safe-area-inset-right));

        --panel-bg: linear-gradient(180deg, rgba(8,12,24,.92), rgba(10,14,30,.84));
        --panel-border: rgba(255,255,255,.12);
        --panel-glow: 0 18px 40px rgba(0,0,0,.34), 0 0 24px rgba(0,247,255,.06);
        --panel-blur: blur(14px) saturate(1.08);
        --text-soft: rgba(255,255,255,.72);
        --text-mid: rgba(255,255,255,.86);
      }

      body.oh-hud-rebuild{
        --mobile-bottom-stack: 188px;
      }

      #ui,
      #bossBarWrap,
      #minimapWrap,
      #weaponBar,
      #abilityDock,
      #tapHint,
      #mobileControls{
        transition:
          top .18s ease,
          right .18s ease,
          left .18s ease,
          bottom .18s ease,
          width .18s ease,
          height .18s ease,
          transform .18s ease,
          opacity .18s ease;
      }

      body.oh-hud-rebuild #nemesisHud,
      body.oh-hud-rebuild #apocHud{
        display:none !important;
      }
      body.oh-hud-rebuild #directorHud,
      body.oh-hud-rebuild #metaHud{
        display:none !important;
      }

      body.oh-hud-rebuild #ui{
        position: fixed !important;
        top: var(--hud-top) !important;
        left: 50% !important;
        right: auto !important;
        bottom: auto !important;
        transform: translateX(-50%) !important;
        width: min(1180px, calc(100vw - 24px)) !important;
        max-width: calc(100vw - 24px) !important;
        min-width: 0 !important;
        display: grid !important;
        grid-template-columns: 260px 1fr 250px !important;
        gap: 12px !important;
        align-items: stretch !important;
        padding: 12px !important;
        border-radius: 20px !important;
        background: var(--panel-bg) !important;
        border: 1px solid var(--panel-border) !important;
        box-shadow: var(--panel-glow) !important;
        backdrop-filter: var(--panel-blur) !important;
        z-index: 30 !important;
        pointer-events: none !important;
        overflow: hidden !important;
      }

      body.oh-hud-rebuild #ui::before{
        content:"" !important;
        position:absolute !important;
        inset:0 !important;
        border-radius:inherit !important;
        pointer-events:none !important;
        background:
          radial-gradient(circle at 10% 0%, rgba(0,247,255,.12), transparent 30%),
          radial-gradient(circle at 100% 0%, rgba(255,79,216,.10), transparent 28%),
          linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,0)) !important;
      }

      body.oh-hud-rebuild #hudLeftMeta,
      body.oh-hud-rebuild #hudRightMeta,
      body.oh-hud-rebuild #brand,
      body.oh-hud-rebuild #hud{
        position: relative !important;
        z-index: 1 !important;
      }

      body.oh-hud-rebuild #hudLeftMeta,
      body.oh-hud-rebuild #hudRightMeta{
        display:flex !important;
        flex-direction:column !important;
        gap:8px !important;
      }

      body.oh-hud-rebuild #brand{
        margin: 0 !important;
        display:flex !important;
        align-items:center !important;
        gap:12px !important;
      }

      body.oh-hud-rebuild #brandMark{
        width: 46px !important;
        height: 46px !important;
        border-radius: 14px !important;
        flex: 0 0 auto !important;
        box-shadow: 0 0 18px rgba(0,247,255,.10) !important;
      }

      body.oh-hud-rebuild #brandText b{
        display:block !important;
        font-size: 16px !important;
        line-height: 1.05 !important;
        letter-spacing: .04em !important;
      }

      body.oh-hud-rebuild #brandText span{
        display:block !important;
        font-size: 11px !important;
        color: var(--text-soft) !important;
      }

      body.oh-hud-rebuild #hud{
        display:grid !important;
        grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
        gap: 8px !important;
        align-content: start !important;
      }

      body.oh-hud-rebuild .stat,
      body.oh-hud-rebuild .uh-card{
        min-width: 0 !important;
        padding: 10px 12px !important;
        border-radius: 14px !important;
        background: linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04)) !important;
        border: 1px solid rgba(255,255,255,.09) !important;
        box-shadow: inset 0 0 14px rgba(255,255,255,.02), 0 8px 16px rgba(0,0,0,.12) !important;
      }

      body.oh-hud-rebuild .stat .label,
      body.oh-hud-rebuild .uh-label{
        display:block !important;
        margin-bottom: 6px !important;
        font-size: 10px !important;
        line-height: 1 !important;
        letter-spacing: .08em !important;
        text-transform: uppercase !important;
        color: var(--text-soft) !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
      }

      body.oh-hud-rebuild .stat .value{
        display:block !important;
        font-size: 18px !important;
        line-height: 1 !important;
        font-weight: 900 !important;
        color: #fff !important;
        white-space: nowrap !important;
        overflow: hidden !important;
        text-overflow: ellipsis !important;
      }

      body.oh-hud-rebuild .stat:nth-child(1),
      body.oh-hud-rebuild .stat:nth-child(2),
      body.oh-hud-rebuild .stat:nth-child(3){
        border-color: rgba(0,247,255,.16) !important;
        background: linear-gradient(180deg, rgba(0,247,255,.10), rgba(255,255,255,.04)) !important;
      }

      body.oh-hud-rebuild .uh-main{
        display:flex !important;
        justify-content:space-between !important;
        align-items:center !important;
        gap:10px !important;
        font-size: 14px !important;
        font-weight: 900 !important;
        color:#fff !important;
      }

      body.oh-hud-rebuild .uh-sub{
        margin-top: 6px !important;
        font-size: 11px !important;
        line-height: 1.3 !important;
        color: var(--text-mid) !important;
      }

      body.oh-hud-rebuild .uh-meter{
        margin-top: 8px !important;
        height: 8px !important;
        border-radius: 999px !important;
        overflow: hidden !important;
        background: rgba(255,255,255,.08) !important;
        border: 1px solid rgba(255,255,255,.10) !important;
      }

      body.oh-hud-rebuild .uh-meter > i{
        display:block !important;
        width:0% !important;
        height:100% !important;
        background: linear-gradient(90deg, #ff4fd8, #8b5cf6, #00f7ff) !important;
      }

      body.oh-hud-rebuild .uh-actions{
        display:flex !important;
        gap:8px !important;
        margin-top: 8px !important;
        pointer-events:auto !important;
      }

      body.oh-hud-rebuild .uh-btn,
      body.oh-hud-rebuild #hudCompactBtn{
        min-height: 38px !important;
        border: 1px solid rgba(255,255,255,.12) !important;
        border-radius: 12px !important;
        background: rgba(255,255,255,.08) !important;
        color:#fff !important;
        font-weight:800 !important;
      }

      body.oh-hud-rebuild .uh-btn{
        flex:1 !important;
      }

      body.oh-hud-rebuild #bossBarWrap{
        position: fixed !important;
        top: calc(var(--hud-top) + 108px) !important;
        left: 50% !important;
        right: auto !important;
        bottom: auto !important;
        transform: translateX(-50%) !important;
        width: min(760px, calc(100vw - 40px)) !important;
        z-index: 28 !important;
      }

      body.oh-hud-rebuild #minimapWrap{
        position: fixed !important;
        top: var(--hud-top) !important;
        right: var(--hud-right) !important;
        left: auto !important;
        bottom: auto !important;
        width: 220px !important;
        height: 220px !important;
        border-radius: 22px !important;
        z-index: 28 !important;
      }

      body.oh-hud-rebuild #weaponBar{
        position: fixed !important;
        left: 50% !important;
        right: auto !important;
        top: auto !important;
        bottom: var(--hud-bottom) !important;
        transform: translateX(-50%) !important;
        z-index: 28 !important;
      }

      body.oh-hud-rebuild #abilityDock{
        position: fixed !important;
        right: var(--hud-right) !important;
        left: auto !important;
        top: auto !important;
        bottom: calc(var(--hud-bottom) + 86px) !important;
        z-index: 28 !important;
        gap: 10px !important;
      }

      body.oh-hud-rebuild .ability-btn{
        min-width: 84px !important;
        min-height: 52px !important;
        border-radius: 16px !important;
        padding: 10px 12px !important;
        font-size: 12px !important;
        font-weight: 800 !important;
      }

      body.oh-hud-rebuild #tapHint{
        right: var(--hud-right) !important;
        left: auto !important;
        bottom: calc(var(--hud-bottom) + 208px) !important;
        z-index: 28 !important;
        font-size: 12px !important;
      }

      body.oh-hud-rebuild #mobileControls{
        z-index: 27 !important;
      }

      body.oh-hud-rebuild #hudCompactBtn{
        position: fixed !important;
        top: calc(var(--hud-top) + 8px) !important;
        right: var(--hud-right) !important;
        z-index: 60 !important;
        padding: 8px 10px !important;
        backdrop-filter: blur(10px) !important;
        pointer-events: auto !important;
      }

      @media (max-width: 1280px){
        body.oh-hud-rebuild #ui{
          width: calc(100vw - 20px) !important;
          max-width: calc(100vw - 20px) !important;
          grid-template-columns: 220px 1fr 220px !important;
        }

        body.oh-hud-rebuild #hud{
          grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
        }
      }

      @media (max-width: 980px){
        body.oh-hud-rebuild{
          --mobile-bottom-stack: 174px;
        }

        body.oh-hud-rebuild #ui{
          width: calc(100vw - 16px) !important;
          max-width: calc(100vw - 16px) !important;
          grid-template-columns: 1fr !important;
          gap: 8px !important;
          padding: 10px !important;
          border-radius: 18px !important;
        }

        body.oh-hud-rebuild #bossBarWrap{
          top: calc(var(--hud-top) + 94px) !important;
          width: calc(100vw - 16px) !important;
        }

        body.oh-hud-rebuild #minimapWrap{
          top: auto !important;
          width: 150px !important;
          height: 180px !important;
          bottom: calc(var(--hud-bottom) + 168px) !important;
        }

        body.oh-hud-rebuild #hudCompactBtn{
          top: auto !important;
          bottom: calc(var(--hud-bottom) + 18px) !important;
        }
      }

      @media (max-width: 640px){
        body.oh-hud-rebuild{
          --mobile-bottom-stack: 168px;
        }

        body.oh-hud-rebuild #ui{
          width: calc(100vw - 12px) !important;
          max-width: calc(100vw - 12px) !important;
          padding: 8px !important;
          border-radius: 16px !important;
        }

        body.oh-hud-rebuild #brandText span,
        body.oh-hud-rebuild #hudLeftMeta,
        body.oh-hud-rebuild #hudRightMeta{
          display:none !important;
        }

        body.oh-hud-rebuild #brandMark{
          width: 38px !important;
          height: 38px !important;
          border-radius: 12px !important;
        }

        body.oh-hud-rebuild #brandText b{
          font-size: 13px !important;
        }

        body.oh-hud-rebuild #hud{
          grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
          gap: 5px !important;
        }

        body.oh-hud-rebuild .stat{
          padding: 8px 7px !important;
          border-radius: 11px !important;
        }

        body.oh-hud-rebuild .stat .label{
          font-size: 8px !important;
        }

        body.oh-hud-rebuild .stat .value{
          font-size: 14px !important;
        }

        body.oh-hud-rebuild .stat:nth-child(n+5){
          display:none !important;
        }

        body.oh-hud-rebuild #bossBarWrap{
          top: calc(var(--hud-top) + 78px) !important;
          width: calc(100vw - 12px) !important;
        }

        body.oh-hud-rebuild #minimapWrap{
          width: 132px !important;
          height: 162px !important;
          bottom: calc(var(--hud-bottom) + 150px) !important;
          border-radius: 16px !important;
        }

        body.oh-hud-rebuild #weaponBar{
          transform: translateX(-50%) scale(.96) !important;
          transform-origin: center bottom !important;
        }

        body.oh-hud-rebuild .ability-btn{
          min-width: 64px !important;
          min-height: 44px !important;
          padding: 8px 9px !important;
          border-radius: 13px !important;
          font-size: 10px !important;
        }
      }

      @media (pointer: coarse) and (orientation: landscape) and (max-height: 560px){
        body.oh-hud-rebuild{
          --mobile-bottom-stack: 112px;
        }

        body.oh-hud-rebuild #ui{
          top: calc(8px + env(safe-area-inset-top)) !important;
          width: min(50vw, 520px) !important;
          max-width: min(50vw, 520px) !important;
          grid-template-columns: 1fr !important;
          padding: 8px !important;
          border-radius: 14px !important;
        }

        body.oh-hud-rebuild #hudLeftMeta,
        body.oh-hud-rebuild #hudRightMeta,
        body.oh-hud-rebuild #brandText span{
          display:none !important;
        }

        body.oh-hud-rebuild #brandMark{
          width: 28px !important;
          height: 28px !important;
          border-radius: 9px !important;
        }

        body.oh-hud-rebuild #brandText b{
          font-size: 11px !important;
        }

        body.oh-hud-rebuild #hud{
          grid-template-columns: repeat(4, minmax(0, 1fr)) !important;
          gap: 4px !important;
        }

        body.oh-hud-rebuild .stat{
          padding: 6px !important;
          border-radius: 10px !important;
        }

        body.oh-hud-rebuild .stat .label{
          font-size: 7px !important;
          margin-bottom: 2px !important;
        }

        body.oh-hud-rebuild .stat .value{
          font-size: 11px !important;
        }

        body.oh-hud-rebuild #bossBarWrap{
          top: calc(8px + env(safe-area-inset-top) + 68px) !important;
          width: min(46vw, 340px) !important;
        }

        body.oh-hud-rebuild #minimapWrap{
          top: auto !important;
          bottom: calc(8px + env(safe-area-inset-bottom) + 96px) !important;
          right: 8px !important;
          width: 110px !important;
          height: 132px !important;
          border-radius: 14px !important;
        }

        body.oh-hud-rebuild #weaponBar{
          bottom: 6px !important;
          transform: translateX(-50%) scale(.90) !important;
        }

        body.oh-hud-rebuild .ability-btn{
          min-width: 56px !important;
          min-height: 38px !important;
          padding: 6px 8px !important;
          font-size: 9px !important;
        }
      }

      body.oh-hud-rebuild.oh-hud-tight #brandText span,
      body.oh-hud-rebuild.oh-hud-tight #hudLeftMeta,
      body.oh-hud-rebuild.oh-hud-tight #hudRightMeta{
        display:none !important;
      }

      body.oh-hud-rebuild.oh-hud-compact #minimapWrap{
        opacity:.55 !important;
        transform: scale(.92) !important;
        transform-origin: top right !important;
      }

      body.oh-hud-rebuild.oh-hud-compact #bossBarWrap{
        opacity:.88 !important;
      }

      body.oh-hud-rebuild.oh-hud-compact #weaponBar{
        transform: translateX(-50%) scale(.94) !important;
      }
    `;
    document.head.appendChild(style);
  }

  function buildUnifiedHudPanels(){
    const ui = document.getElementById("ui");
    const brand = document.getElementById("brand");
    const hud = document.getElementById("hud");
    if(!ui || !brand || !hud || document.getElementById("hudLeftMeta")) return;

    const left = document.createElement("div");
    left.id = "hudLeftMeta";
    left.innerHTML = `
      <div class="uh-card">
        <div class="uh-label">Arena event</div>
        <div class="uh-main">
          <span id="uhEventName">Geen event</span>
          <span id="uhEventTime">0.0s</span>
        </div>
        <div class="uh-sub" id="uhEventDesc">De arena is stabiel.</div>
      </div>
      <div class="uh-card">
        <div class="uh-label">Boss phase</div>
        <div class="uh-main">
          <span id="uhBossState">Geen baas</span>
          <span id="uhBossPhase">-</span>
        </div>
        <div class="uh-sub" id="uhBossDesc">Nog geen Nemesis actief.</div>
      </div>
    `;

    const center = document.createElement("div");
    center.id = "hudCenterStack";
    center.appendChild(brand);
    center.appendChild(hud);

    const right = document.createElement("div");
    right.id = "hudRightMeta";
    right.innerHTML = `
      <div class="uh-card">
        <div class="uh-main">
          <span>FURY MODE</span>
          <span id="uhFuryPct">0%</span>
        </div>
        <div class="uh-sub" id="uhFuryText">Maak kills om Fury op te laden</div>
        <div class="uh-meter"><i id="uhFuryFill"></i></div>
        <div class="uh-actions">
          <button id="uhFuryBtn" class="uh-btn" type="button">Q / V Fury</button>
          <button id="uhDashBtn" class="uh-btn" type="button">Dash</button>
        </div>
      </div>
    `;

    ui.innerHTML = "";
    ui.append(left, center, right);

    const furyBtn = document.getElementById("uhFuryBtn");
    const dashBtn = document.getElementById("uhDashBtn");

    furyBtn?.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if(typeof activateFury === "function") activateFury();
    });

    dashBtn?.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if(typeof doDash === "function") doDash();
    });
  }

  function syncUnifiedHud(){
    const mapText = (fromId, toId) => {
      const from = document.getElementById(fromId);
      const to = document.getElementById(toId);
      if(from && to) to.textContent = from.textContent;
    };

    const mapWidth = (fromId, toId) => {
      const from = document.getElementById(fromId);
      const to = document.getElementById(toId);
      if(from && to) to.style.setProperty("width", from.style.width || "0%", "important");
    };

    const tick = () => {
      if(!document.body.classList.contains("oh-hud-rebuild")) return;
      mapText("nemEventName", "uhEventName");
      mapText("nemEventTime", "uhEventTime");
      mapText("nemEventDesc", "uhEventDesc");
      mapText("nemBossState", "uhBossState");
      mapText("nemBossPhase", "uhBossPhase");
      mapText("nemBossDesc", "uhBossDesc");
      mapText("apocFuryPct", "uhFuryPct");
      mapText("apocFuryText", "uhFuryText");
      mapWidth("apocFuryFill", "uhFuryFill");
      requestAnimationFrame(tick);
    };

    tick();
  }

  function normalizeHudPanels(){
    const ui = document.getElementById("ui");
    const nem = document.getElementById("nemesisHud");
    const apoc = document.getElementById("apocHud");
    const boss = document.getElementById("bossBarWrap");
    const map = document.getElementById("minimapWrap");
    const weaponBar = document.getElementById("weaponBar");
    const abilityDock = document.getElementById("abilityDock");

    if(ui) ui.style.pointerEvents = "none";
    if(nem) nem.style.pointerEvents = "none";
    if(apoc) apoc.style.pointerEvents = "none";
    if(boss) boss.style.pointerEvents = "none";
    if(map) map.style.pointerEvents = "none";

    if(weaponBar){
      weaponBar.style.left = "";
      weaponBar.style.right = "";
      weaponBar.style.top = "";
    }

    if(abilityDock){
      abilityDock.style.left = "";
      abilityDock.style.top = "";
    }
  }

  function tightenHudIfNeeded(){
    const tight = window.innerWidth < 1120 || window.innerHeight < 720;
    document.body.classList.toggle("oh-hud-tight", tight);
  }

  function ensureHudToggle(){
    if(document.getElementById("hudCompactBtn")) return;
    const btn = document.createElement("button");
    btn.id = "hudCompactBtn";
    btn.type = "button";
    btn.textContent = "HUD";
    btn.addEventListener("click", () => {
      document.body.classList.toggle("oh-hud-compact");
    });
    document.body.appendChild(btn);
  }

  function rebuildHud(){
    if(document.getElementById("ohHudRebuildStyles")) return;
    injectHudRebuildStyles();
    document.body.classList.add("oh-hud-rebuild");
    normalizeHudPanels();
    buildUnifiedHudPanels();
    syncUnifiedHud();
    ensureHudToggle();
    tightenHudIfNeeded();
  }

  rebuildHud();
  window.addEventListener("resize", tightenHudIfNeeded, { passive: true });
  window.addEventListener("orientationchange", tightenHudIfNeeded, { passive: true });
})();

/* =========================
   OLDE HANTER RELIC ARMORY PACK
   plak dit direct boven: animate(performance.now());
   ========================= */
(() => {
  const ARMORY_KEY = "oldehanter_relic_armory_v1";

  const armory = {
    credits: 0,
    earnedThisRun: 0,
    relicsTaken: 0,
    rerolls: 1,
    awaitingWaveStart: false,
    draftOpen: false,
    continueFn: null,
    reviveUsed: false,
    levels: {
      overclock: 0,     // damage
      rapidfire: 0,     // fire rate
      plating: 0,       // resist
      frame: 0,         // max hp
      magboots: 0,      // speed
      vamp: 0,          // heal on kill
      printer: 0,       // ammo packages
      capacitor: 0,     // ability stocks / blast radius
      combo: 0,         // combo duration/score bonus
      echo: 0,          // chance on extra side shot
      scavenger: 0,     // more credits
      phoenix: 0        // once-per-run revive
    },
    profile: loadProfile(),
    hud: {},
    draft: {
      root: null,
      cards: null,
      title: null,
      sub: null,
      continueBtn: null,
      rerollBtn: null
    }
  };

  function loadProfile(){
    try{
      return JSON.parse(localStorage.getItem(ARMORY_KEY)) || {
        lifetimeCredits: 0,
        totalRelics: 0,
        runs: 0,
        bestWave: 1,
        bestScore: 0
      };
    }catch{
      return {
        lifetimeCredits: 0,
        totalRelics: 0,
        runs: 0,
        bestWave: 1,
        bestScore: 0
      };
    }
  }

  function saveProfile(){
    try{
      localStorage.setItem(ARMORY_KEY, JSON.stringify(armory.profile));
    }catch{}
  }

  function aClamp(v, a, b){ return Math.max(a, Math.min(b, v)); }
  function aRand(a, b){ return a + Math.random() * (b - a); }
  function aPick(arr){ return arr[Math.floor(Math.random() * arr.length)]; }
  function aShuffle(arr){
    const copy = arr.slice();
    for(let i = copy.length - 1; i > 0; i--){
      const j = Math.floor(Math.random() * (i + 1));
      [copy[i], copy[j]] = [copy[j], copy[i]];
    }
    return copy;
  }
  function fmt(n){ return Math.round(n).toLocaleString("nl-NL"); }

  function addArmoryStyles(){
    const style = document.createElement("style");
    style.id = "ohRelicArmoryStyles";
    style.textContent = `
      #armoryHud{
        position:fixed;
        left:16px;
        bottom:16px;
        z-index:26;
        width:min(330px, calc(100vw - 32px));
        display:flex;
        flex-direction:column;
        gap:10px;
        pointer-events:none;
      }
      .armory-card{
        pointer-events:auto;
        border-radius:18px;
        padding:12px 14px;
        color:#fff;
        background:linear-gradient(180deg, rgba(8,10,22,.82), rgba(15,8,30,.68));
        border:1px solid rgba(255,255,255,.12);
        box-shadow:0 16px 34px rgba(0,0,0,.28), 0 0 24px rgba(255,230,0,.08);
        backdrop-filter:blur(14px) saturate(1.08);
      }
      .armory-top{
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:10px;
        font-weight:900;
      }
      .armory-kicker{
        font-size:.76rem;
        letter-spacing:.14em;
        text-transform:uppercase;
        color:rgba(255,255,255,.72);
      }
      .armory-credits{
        display:inline-flex;
        align-items:center;
        gap:8px;
        font-size:1rem;
      }
      .armory-chip{
        padding:.32rem .7rem;
        border-radius:999px;
        background:linear-gradient(90deg, rgba(255,209,102,.22), rgba(255,79,216,.18));
        border:1px solid rgba(255,255,255,.12);
        box-shadow:0 0 18px rgba(255,209,102,.10);
      }
      .armory-sub{
        margin-top:8px;
        color:rgba(255,255,255,.76);
        font-size:.84rem;
        font-weight:700;
      }
      .armory-list{
        display:grid;
        grid-template-columns:repeat(2,minmax(0,1fr));
        gap:8px;
        margin-top:10px;
      }
      .armory-pill{
        padding:.55rem .7rem;
        border-radius:14px;
        background:rgba(255,255,255,.06);
        border:1px solid rgba(255,255,255,.08);
        font-size:.8rem;
        font-weight:800;
        color:#fff;
      }

      #relicDraft{
        position:fixed;
        inset:0;
        z-index:60;
        display:none;
        align-items:center;
        justify-content:center;
        padding:18px;
        background:
          radial-gradient(circle at 50% 50%, rgba(255,255,255,.05), transparent 32%),
          rgba(2,4,10,.72);
        backdrop-filter: blur(10px) saturate(1.06);
      }
      #relicDraft.show{ display:flex; }

      .relic-shell{
        width:min(1120px, 100%);
        max-height:min(92vh, 920px);
        overflow:auto;
        border-radius:26px;
        padding:18px;
        background:linear-gradient(180deg, rgba(9,10,24,.94), rgba(16,10,34,.88));
        border:1px solid rgba(255,255,255,.12);
        box-shadow:0 22px 60px rgba(0,0,0,.35), 0 0 34px rgba(138,46,255,.14);
      }
      .relic-head{
        display:flex;
        align-items:flex-start;
        justify-content:space-between;
        gap:14px;
        flex-wrap:wrap;
      }
      .relic-title{
        color:#fff;
        font-size:clamp(1.4rem, 3vw, 2rem);
        font-weight:900;
        letter-spacing:.02em;
      }
      .relic-meta{
        color:rgba(255,255,255,.72);
        font-weight:700;
        line-height:1.35;
      }
      .relic-actions{
        display:flex;
        align-items:center;
        gap:10px;
        flex-wrap:wrap;
      }
      .relic-btn{
        appearance:none;
        border:0;
        cursor:pointer;
        border-radius:999px;
        padding:.82rem 1rem;
        font-weight:900;
        color:#fff;
        background:linear-gradient(90deg,#ff00a8,#8a2eff,#00f7ff,#ffe600,#ff00a8);
        background-size:240% 240%;
        animation:relicFlow 4.5s linear infinite;
        box-shadow:0 0 18px rgba(255,0,168,.20), 0 0 28px rgba(0,247,255,.12);
      }
      .relic-btn.secondary{
        background:rgba(255,255,255,.08);
        border:1px solid rgba(255,255,255,.14);
        box-shadow:none;
        animation:none;
      }
      .relic-btn:disabled{
        filter:grayscale(.75) brightness(.7);
        cursor:not-allowed;
        box-shadow:none;
      }
      .relic-grid{
        margin-top:18px;
        display:grid;
        grid-template-columns:repeat(3, minmax(0, 1fr));
        gap:14px;
      }
      .relic-card{
        border-radius:22px;
        padding:16px;
        background:
          linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04));
        border:1px solid rgba(255,255,255,.12);
        box-shadow: inset 0 0 18px rgba(255,255,255,.02), 0 10px 24px rgba(0,0,0,.20);
        color:#fff;
        display:flex;
        flex-direction:column;
        gap:10px;
        min-height:240px;
      }
      .relic-rarity{
        font-size:.76rem;
        letter-spacing:.16em;
        text-transform:uppercase;
        color:rgba(255,255,255,.68);
        font-weight:900;
      }
      .relic-name{
        font-size:1.18rem;
        font-weight:900;
        line-height:1.12;
      }
      .relic-desc{
        color:rgba(255,255,255,.78);
        font-size:.92rem;
        line-height:1.45;
        flex:1;
      }
      .relic-tagline{
        font-size:.8rem;
        color:rgba(255,255,255,.62);
        font-weight:700;
      }
      .relic-buy{
        margin-top:auto;
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:10px;
      }
      .relic-cost{
        font-weight:900;
        color:#ffd166;
      }
      .relic-card[data-rarity="epic"]{
        box-shadow: inset 0 0 18px rgba(255,255,255,.02), 0 0 26px rgba(255,0,168,.10);
      }
      .relic-card[data-rarity="legend"]{
        box-shadow: inset 0 0 18px rgba(255,255,255,.02), 0 0 28px rgba(255,230,0,.12);
      }
      .relic-note{
        margin-top:14px;
        color:rgba(255,255,255,.72);
        font-size:.86rem;
        font-weight:700;
      }

      @keyframes relicFlow{
        0%{ background-position:0% 50%; }
        100%{ background-position:200% 50%; }
      }

      @media (max-width: 980px){
        .relic-grid{ grid-template-columns:1fr; }
      }

      @media (max-width: 720px){
        #armoryHud{
          left:10px;
          bottom:10px;
          width:min(290px, calc(100vw - 20px));
        }
        .armory-list{
          grid-template-columns:1fr 1fr;
        }
        .relic-shell{
          padding:14px;
          border-radius:22px;
        }
      }
    `;
    document.head.appendChild(style);
  }

  function buildHud(){
    addArmoryStyles();

    const hud = document.createElement("div");
    hud.id = "armoryHud";
    hud.innerHTML = `
      <div class="armory-card">
        <div class="armory-kicker">Relic Armory</div>
        <div class="armory-top">
          <div class="armory-credits">
            <span class="armory-chip">Salvage: <b id="armoryCredits">0</b></span>
            <span class="armory-chip">Relics: <b id="armoryRelics">0</b></span>
          </div>
          <span class="armory-chip">Runs: <b id="armoryRuns">0</b></span>
        </div>
        <div class="armory-sub" id="armorySub">
          Elke 2 waves krijg je een relic draft. Boss waves geven betere keuzes.
        </div>
        <div class="armory-list" id="armoryList"></div>
      </div>
    `;
    document.body.appendChild(hud);

    armory.hud.root = hud;
    armory.hud.credits = hud.querySelector("#armoryCredits");
    armory.hud.relics = hud.querySelector("#armoryRelics");
    armory.hud.runs = hud.querySelector("#armoryRuns");
    armory.hud.sub = hud.querySelector("#armorySub");
    armory.hud.list = hud.querySelector("#armoryList");

    const draft = document.createElement("div");
    draft.id = "relicDraft";
    draft.innerHTML = `
      <div class="relic-shell">
        <div class="relic-head">
          <div>
            <div class="relic-title" id="relicDraftTitle">Relic Draft</div>
            <div class="relic-meta" id="relicDraftSub">
              Kies een upgrade tussen de waves. Je spel pauzeert totdat je doorgaat.
            </div>
          </div>
          <div class="relic-actions">
            <button id="relicRerollBtn" class="relic-btn secondary" type="button">Reroll</button>
            <button id="relicContinueBtn" class="relic-btn secondary" type="button">Doorgaan</button>
          </div>
        </div>
        <div class="relic-grid" id="relicCards"></div>
        <div class="relic-note">
          Tip: koop damage, fire-rate en plating vroeg. Neem Phoenix alleen als verzekering.
        </div>
      </div>
    `;
    document.body.appendChild(draft);

    armory.draft.root = draft;
    armory.draft.cards = draft.querySelector("#relicCards");
    armory.draft.title = draft.querySelector("#relicDraftTitle");
    armory.draft.sub = draft.querySelector("#relicDraftSub");
    armory.draft.continueBtn = draft.querySelector("#relicContinueBtn");
    armory.draft.rerollBtn = draft.querySelector("#relicRerollBtn");

    armory.draft.continueBtn.addEventListener("click", () => closeDraftAndContinue());
    armory.draft.rerollBtn.addEventListener("click", () => rerollDraft());

    updateHud();
  }

  function activePerkLines(){
    const entries = [];
    const L = armory.levels;
    if(L.overclock) entries.push(`Overclock ${L.overclock}`);
    if(L.rapidfire) entries.push(`Rapid ${L.rapidfire}`);
    if(L.plating) entries.push(`Plating ${L.plating}`);
    if(L.frame) entries.push(`Frame ${L.frame}`);
    if(L.magboots) entries.push(`Mag Boots ${L.magboots}`);
    if(L.vamp) entries.push(`Vamp ${L.vamp}`);
    if(L.printer) entries.push(`Printer ${L.printer}`);
    if(L.capacitor) entries.push(`Capacitor ${L.capacitor}`);
    if(L.combo) entries.push(`Combo ${L.combo}`);
    if(L.echo) entries.push(`Echo ${L.echo}`);
    if(L.scavenger) entries.push(`Scavenger ${L.scavenger}`);
    if(L.phoenix) entries.push(`Phoenix ${L.phoenix}${armory.reviveUsed ? " (used)" : ""}`);
    return entries.length ? entries.slice(0, 6) : ["Nog geen relics"];
  }

  function updateHud(){
    if(!armory.hud.root) return;
    armory.hud.credits.textContent = fmt(armory.credits);
    armory.hud.relics.textContent = fmt(armory.relicsTaken);
    armory.hud.runs.textContent = fmt(armory.profile.runs);
    armory.hud.sub.textContent =
      armory.draftOpen
        ? "Relic Draft open — kies een upgrade of ga verder."
        : `Beste wave: ${fmt(armory.profile.bestWave)} • Beste score: ${fmt(armory.profile.bestScore)}`;

    armory.hud.list.innerHTML = activePerkLines()
      .map(x => `<div class="armory-pill">${x}</div>`)
      .join("");
  }

  function syncDerivedStats(){
    player.maxHp = 100 + armory.levels.frame * 25;
    player.speed = 10.2 * (1 + armory.levels.magboots * 0.12) * (1 + (player.speedBoost || 0));
    if(player.hp > player.maxHp) player.hp = player.maxHp;
    setStat?.();
    updateHud();
  }

  function awardCredits(amount, source=""){
    const bonus = 1 + armory.levels.scavenger * 0.18;
    const finalAmount = Math.max(1, Math.round(amount * bonus));
    armory.credits += finalAmount;
    armory.earnedThisRun += finalAmount;
    armory.profile.lifetimeCredits += finalAmount;
    saveProfile();
    updateHud();
    if(source){
      flashHint?.(`+${finalAmount} salvage • ${source}`, 900);
    }
  }

  function draftOwnedLevel(choice){
    if(choice.levelKey) return armory.levels[choice.levelKey] || 0;
    if(choice.id && Object.prototype.hasOwnProperty.call(armory.levels, choice.id)){
      return armory.levels[choice.id] || 0;
    }
    return 0;
  }

  function draftTagline(choice){
    if(choice.tagline) return choice.tagline;
    if(choice.oneShot) return "Eenmalig effect";
    const lvl = draftOwnedLevel(choice);
    return `Level nu: ${lvl}`;
  }

  function draftResupply(mult = 1){
    player.hp = Math.min(player.maxHp, player.hp + Math.round((12 + armory.levels.frame * 2) * mult));
    player.ammo.bullet += Math.round((8 + armory.levels.printer * 4) * mult);
    player.ammo.rocket += armory.levels.printer > 0 ? Math.max(1, Math.round(mult)) : 0;
    player.ammo.grenade += armory.levels.printer > 1 ? Math.max(1, Math.round(mult)) : 0;

    if(armory.levels.capacitor > 0 && Math.random() < 0.55){
      player.abilities.plasma += 1;
    }
    setStat?.();
  }

  function makeRelic(def){
    return {
      rarity: "rare",
      weight: 1,
      cost: 20,
      needsSync: false,
      oneShot: false,
      levelKey: def.id,
      ...def
    };
  }

  function relicPool(){
    const L = armory.levels;
    const nearBoss = (player.wave + 1) % 5 === 0;
    const lowHp = player.hp <= player.maxHp * 0.45;
    const lowAmmo = player.ammo.bullet <= 20 && player.ammo.rocket <= 1 && player.ammo.grenade <= 1;
    const lowAbilities = (player.abilities.plasma + player.abilities.mine + player.abilities.orbital) <= 2;
    const wave = player.wave;

    const pool = [
      makeRelic({
        id:"overclock",
        rarity:"rare",
        name:"Railspire Injector",
        desc:"+18% projectielschade per niveau. Verhoogt de schade van kogels, rockets en plasma.",
        tagline:"Offensief • schade",
        cost:26 + L.overclock * 10,
        weight: 1.15 + (L.overclock <= 1 ? 0.15 : 0),
        apply(){
          L.overclock += 1;
        }
      }),

      makeRelic({
        id:"rapidfire",
        rarity:"rare",
        name:"Tempest Chamber",
        desc:"+16% vuursnelheid per niveau. Verhoogt de druk op korte en middellange afstand.",
        tagline:"Offensief • vuursnelheid",
        cost:24 + L.rapidfire * 10,
        weight: 1.1 + (L.echo > 0 ? 0.15 : 0),
        apply(){
          L.rapidfire += 1;
        }
      }),

      makeRelic({
        id:"plating",
        rarity:"rare",
        name:"Bastion Weave",
        desc:"-14% inkomende schade per niveau. Eenvoudige maar betrouwbare verdedigingsupgrade.",
        tagline:"Defensief • schadebeperking",
        cost:24 + L.plating * 12,
        weight: lowHp ? 1.45 : 1.0,
        apply(){
          L.plating += 1;
        }
      }),

      makeRelic({
        id:"frame",
        rarity:"epic",
        name:"Titan Chassis",
        desc:"+25 maximale HP en direct 25 HP herstel. Verbetert de foutmarge van je run aanzienlijk.",
        tagline:"Defensief • maximale HP",
        cost:32 + L.frame * 12,
        needsSync: true,
        weight: lowHp ? 1.5 : 0.95,
        apply(){
          L.frame += 1;
          syncDerivedStats();
          player.hp = Math.min(player.maxHp, player.hp + 25);
          setStat?.();
        }
      }),

      makeRelic({
        id:"magboots",
        rarity:"rare",
        name:"Slipstream Soles",
        desc:"+12% bewegingssnelheid per niveau. Handig voor positionering, ontwijken en pickups veiligstellen.",
        tagline:"Mobiliteit • snelheid",
        cost:22 + L.magboots * 10,
        needsSync: true,
        weight: 1.05 + (player.wave >= 4 ? 0.1 : 0),
        apply(){
          L.magboots += 1;
          syncDerivedStats();
        }
      }),

      makeRelic({
        id:"vamp",
        rarity:"epic",
        name:"Crimson Recycler",
        desc:"Herstel 2 HP per kill. Wordt sterker naarmate gevechten drukker worden.",
        tagline:"Sustain • levensherstel",
        cost:30 + L.vamp * 12,
        weight: 1.0 + (player.wave >= 5 ? 0.15 : 0),
        apply(){
          L.vamp += 1;
        }
      }),

      makeRelic({
        id:"printer",
        rarity:"rare",
        name:"Munitions Foundry",
        desc:"Meer munitie tussen waves en direct een extra levering bij aankoop.",
        tagline:"Logistiek • munitie",
        cost:22 + L.printer * 10,
        weight: lowAmmo ? 1.55 : 1.05,
        apply(){
          L.printer += 1;
          player.ammo.bullet += 12;
          player.ammo.rocket += 1;
          player.ammo.grenade += 1;
          setStat?.();
        }
      }),

      makeRelic({
        id:"capacitor",
        rarity:"epic",
        name:"Stormglass Capacitor",
        desc:"+1 charge op alle abilities en grotere explosieradius voor zware projectielen.",
        tagline:"Abilities • capaciteit",
        cost:34 + L.capacitor * 12,
        weight: lowAbilities ? 1.4 : 1.0,
        apply(){
          L.capacitor += 1;
          player.abilities.plasma += 1;
          player.abilities.mine += 1;
          player.abilities.orbital += 1;
          setStat?.();
        }
      }),

      makeRelic({
        id:"combo",
        rarity:"rare",
        name:"Encore Matrix",
        desc:"Verlengt de combo-duur en verhoogt de scoregroei per niveau.",
        tagline:"Offensief • combo",
        cost:24 + L.combo * 10,
        weight: 1.0 + (player.score > 0 ? 0.1 : 0),
        apply(){
          L.combo += 1;
        }
      }),

      makeRelic({
        id:"echo",
        rarity:"epic",
        name:"Mirrorcoil Trigger",
        desc:"Geeft een kans op extra zijdelingse schoten. Past goed in snelle bullet-builds.",
        tagline:"Offensief • extra projectielen",
        cost:30 + L.echo * 14,
        weight: 1.0 + (L.rapidfire > 0 ? 0.2 : 0),
        apply(){
          L.echo += 1;
        }
      }),

      makeRelic({
        id:"scavenger",
        rarity:"rare",
        name:"Graveroute Scanner",
        desc:"+18% meer salvage uit kills, pickups en wave-beloningen.",
        tagline:"Economie • salvage",
        cost:20 + L.scavenger * 10,
        weight: armory.credits < 28 ? 1.3 : 0.95,
        apply(){
          L.scavenger += 1;
        }
      }),

      makeRelic({
        id:"phoenix",
        rarity:"legend",
        name:"Phoenix Protocol",
        desc:"Eenmaal per run: overleef lethale schade en keer terug met 45 HP.",
        tagline:"Legendair • tweede kans",
        cost:42 + L.phoenix * 18,
        weight: armory.reviveUsed ? 0.25 : 0.9,
        apply(){
          L.phoenix += 1;
        }
      }),

      makeRelic({
        id:"fieldkit",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Field Kit Drop",
        desc:"Direct herstelpakket: herstel HP, vul munitie aan en ontvang kans op 1 extra plasma charge.",
        tagline:"Eenmalig • herstelpakket",
        cost:16 + Math.floor(player.wave * 1.5),
        weight: lowHp || lowAmmo ? 1.8 : 0.85,
        apply(){
          player.hp = Math.min(player.maxHp, player.hp + 28);
          player.ammo.bullet += 28;
          player.ammo.rocket += 1;
          player.ammo.grenade += 1;
          if(Math.random() < 0.65) player.abilities.plasma += 1;
          setStat?.();
        }
      }),

      makeRelic({
        id:"bounty",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Black Market Bounty",
        desc:"Ontvang direct salvage en 1 gratis reroll voor de huidige draft.",
        tagline:"Eenmalig • economie",
        cost:18 + Math.floor(player.wave * 2),
        weight: armory.rerolls <= 0 ? 1.5 : 1.0,
        apply(){
          armory.credits += 18 + Math.floor(player.wave * 3);
          armory.rerolls += 1;
        }
      }),

      makeRelic({
        id:"fusion",
        rarity:"epic",
        oneShot:true,
        levelKey:null,
        name:"Fusion Ritual",
        desc:"+1 Overclock en +1 Rapidfire in één keuze. Eenvoudige offensieve bundel.",
        tagline:"Bundel • schade en tempo",
        cost:40 + (L.overclock + L.rapidfire) * 10,
        apply(){
          L.overclock += 1;
          L.rapidfire += 1;
        }
      }),

      makeRelic({
        id:"bulwark",
        rarity:"epic",
        oneShot:true,
        levelKey:null,
        name:"Bulwark Pact",
        desc:"+1 Plating, +1 Frame en direct herstel. Ideaal voor runs die onder druk staan.",
        tagline:"Bundel • verdediging",
        cost:42 + (L.plating + L.frame) * 10,
        needsSync: true,
        weight: lowHp ? 1.55 : 0.8,
        apply(){
          L.plating += 1;
          L.frame += 1;
          syncDerivedStats();
          player.hp = Math.min(player.maxHp, player.hp + 30);
          setStat?.();
        }
      }),

      makeRelic({
        id:"surge",
        rarity:"epic",
        oneShot:true,
        levelKey:null,
        name:"Arc Surge Cache",
        desc:"Vult abilities direct aan: +2 plasma, +1 mine en +1 orbital.",
        tagline:"Bundel • abilities",
        cost:36 + L.capacitor * 8,
        weight: lowAbilities ? 1.55 : 0.85,
        apply(){
          player.abilities.plasma += 2;
          player.abilities.mine += 1;
          player.abilities.orbital += 1;
          setStat?.();
        }
      }),

      makeRelic({
        id:"trickster",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Trickster Deal",
        desc:"+1 Echo, +1 Mag Boots en een kleine salvage-teruggave.",
        tagline:"Bundel • mobiliteit",
        cost:31 + (L.echo + L.magboots) * 9,
        needsSync: true,
        weight: 1.0 + (L.echo === 0 ? 0.15 : 0),
        apply(){
          L.echo += 1;
          L.magboots += 1;
          armory.credits += 8;
          syncDerivedStats();
        }
      }),

      makeRelic({
        id:"moonKey",
        rarity:"epic",
        oneShot:true,
        levelKey:null,
        name:"Lunar Calibration Key",
        desc:"Een rocket die de maan raakt, wist direct de volledige huidige wave. Werkt ook op bosses.",
        tagline:"Special • wave skip",
        cost:34 + Math.floor(wave * 1.8),
        weight: wave >= 4 ? 1.05 : 0.25,
        apply(){
          state.moonNukeWave = 0;
          flashHint?.("Lunar calibration gereed");
        }
      }),

      makeRelic({
        id:"supplyLine",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Quartermaster Note",
        desc:"Ontvang 2 rockets, 2 granaten en 20 kogels. Functioneel en direct bruikbaar.",
        tagline:"Eenmalig • zware munitie",
        cost:17 + Math.floor(wave * 1.2),
        weight: lowAmmo ? 1.75 : 0.9,
        apply(){
          player.ammo.bullet += 20;
          player.ammo.rocket += 2;
          player.ammo.grenade += 2;
          setStat?.();
        }
      }),

      makeRelic({
        id:"backspin",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Unsafe Prototype",
        desc:"Experimenteel pakket: +1 rocket, +1 plasma charge en 6 salvage. Niet elegant, wel nuttig.",
        tagline:"Curiositeit • experimenteel",
        cost:11 + Math.floor(wave * 0.8),
        weight: 0.95,
        apply(){
          player.ammo.rocket += 1;
          player.abilities.plasma += 1;
          armory.credits += 6;
          flashHint?.("Prototype bleef heel");
          setStat?.();
        }
      }),

      makeRelic({
        id:"stageNotes",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Arena Director Notes",
        desc:"Een stapel managementnotities. Geeft 10 salvage, vooral omdat iemand ze moet opruimen.",
        tagline:"Grappig • administratie",
        cost:6 + Math.floor(wave * 0.6),
        weight: 0.75,
        apply(){
          armory.credits += 10;
          flashHint?.("Notities uit roulatie gehaald");
        }
      }),

      makeRelic({
        id:"coffee",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Night Shift Coffee",
        desc:"Herstel 12 HP en ontvang tijdelijk extra bewegingssnelheid voor de volgende wave.",
        tagline:"Special • tijdelijke boost",
        cost:13 + Math.floor(wave),
        weight: lowHp ? 1.2 : 0.9,
        apply(){
          player.hp = Math.min(player.maxHp, player.hp + 12);
          player.speedBoost = Math.max(player.speedBoost || 0, 0.08);
          player.speedBoostTimer = Math.max(player.speedBoostTimer || 0, 45);
          setStat?.();
          flashHint?.("Koffie actief");
        }
      }),

      makeRelic({
        id:"satchel",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Auxiliary Satchel",
        desc:"Verhoogt direct je voorraad: +1 plasma, +1 mine, +1 orbital en +1 grenade.",
        tagline:"Special • tactische reserve",
        cost:24 + Math.floor(wave * 1.4),
        weight: lowAbilities ? 1.45 : 0.95,
        apply(){
          player.abilities.plasma += 1;
          player.abilities.mine += 1;
          player.abilities.orbital += 1;
          player.ammo.grenade += 1;
          setStat?.();
        }
      })
    ];

    pool.push(
      makeRelic({
        id:"surveyDrone",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Survey Drone",
        desc:"Scant de arena en betaalt direct 14 salvage uit. Daarnaast ontvang je 1 gratis reroll.",
        tagline:"Utility • informatie en economie",
        cost:15 + Math.floor(wave * 1.1),
        weight: armory.rerolls <= 0 ? 1.35 : 0.95,
        apply(){
          armory.credits += 14;
          armory.rerolls += 1;
          flashHint?.("Survey Drone leverde salvage op");
        }
      }),
      makeRelic({
        id:"afterburner",
        rarity:"rare",
        oneShot:true,
        levelKey:null,
        name:"Afterburner Capsule",
        desc:"Voor de volgende wave: extra bewegingssnelheid, +1 rocket en +1 grenade.",
        tagline:"Mobiliteit • tijdelijke boost",
        cost:18 + Math.floor(wave * 1.3),
        weight: 1.0,
        apply(){
          player.speedBoost = Math.max(player.speedBoost || 0, 0.12);
          player.speedBoostTimer = Math.max(player.speedBoostTimer || 0, 55);
          player.ammo.rocket += 1;
          player.ammo.grenade += 1;
          setStat?.();
        }
      }),
      makeRelic({
        id:"paperwork",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Approved Paperwork",
        desc:"Volledig nutteloze arena-administratie. Geeft wel 9 salvage en een verrassend goed gevoel van orde.",
        tagline:"Curiositeit • lichte economie",
        cost:5 + Math.floor(wave * 0.5),
        weight: 0.7,
        apply(){
          armory.credits += 9;
          flashHint?.("Papierwerk verwerkt");
        }
      }),
      makeRelic({
        id:"confettiWarhead",
        rarity:"common",
        oneShot:true,
        levelKey:null,
        name:"Confetti Warhead",
        desc:"+1 rocket. De explosie is volgens de handleiding 'feestelijk verantwoord'.",
        tagline:"Grappig • extra rocket",
        cost:10 + Math.floor(wave),
        weight: lowAmmo ? 1.2 : 0.85,
        apply(){
          player.ammo.rocket += 1;
          setStat?.();
        }
      })
    );

    if(nearBoss){
      pool.push(
        makeRelic({
          id:"apex",
          rarity:"legend",
          oneShot:true,
          levelKey:null,
          name:"Apex Core",
          desc:"+1 Overclock, +1 Capacitor, +1 Combo en een volledige resupply voor de boss wave.",
          tagline:"Legendair • bossvoorbereiding",
          cost:58 + Math.floor(player.wave * 2.5),
          apply(){
            L.overclock += 1;
            L.capacitor += 1;
            L.combo += 1;
            player.abilities.plasma += 1;
            player.abilities.mine += 1;
            draftResupply(1.15);
          }
        }),
        makeRelic({
          id:"ceasefire",
          rarity:"epic",
          oneShot:true,
          levelKey:null,
          name:"Emergency Ceasefire",
          desc:"Start de volgende boss wave met minder reguliere adds en extra ability charges.",
          tagline:"Special • bosscontrole",
          cost:41 + Math.floor(player.wave * 1.7),
          apply(){
            player.abilities.plasma += 2;
            player.abilities.mine += 1;
            player.abilities.orbital += 1;
            state.preBossRelief = true;
            setStat?.();
          }
        })
      );
    }

    return pool;
  }

  function draftWeightedPick(pool, usedIds){
    let filtered = pool.filter(choice => !usedIds.has(choice.id));
    if(!filtered.length) filtered = pool.slice();

    const total = filtered.reduce((sum, item) => sum + Math.max(0.01, item.weight || 1), 0);
    let roll = Math.random() * total;

    for(const item of filtered){
      roll -= Math.max(0.01, item.weight || 1);
      if(roll <= 0) return item;
    }
    return filtered[filtered.length - 1];
  }

  function generateDraftChoices(){
    const pool = relicPool();
    const used = new Set();
    const results = [];
    const nextWave = player.wave + 1;
    const isMajor = nextWave % 5 === 0;

    const legendChance = isMajor ? 0.42 : 0.12;
    const epicChance = isMajor ? 0.72 : 0.38;

    const legendaryPool = pool.filter(x => x.rarity === "legend");
    const epicPool = pool.filter(x => x.rarity === "epic");
    const rarePool = pool.filter(x => x.rarity === "rare" || x.rarity === "common");

    if(legendaryPool.length && Math.random() < legendChance){
      const pick = draftWeightedPick(legendaryPool, used);
      results.push(pick);
      used.add(pick.id);
    }

    if(results.length < 3 && epicPool.length && Math.random() < epicChance){
      const pick = draftWeightedPick(epicPool, used);
      results.push(pick);
      used.add(pick.id);
    }

    while(results.length < 3){
      const source = Math.random() < 0.6 ? rarePool : pool;
      const pick = draftWeightedPick(source, used);
      results.push(pick);
      used.add(pick.id);
    }

    armory.currentChoices = aShuffle(results);
    renderDraftChoices();
  }

  function renderDraftChoices(){
    const choices = armory.currentChoices || [];

    armory.draft.cards.innerHTML = choices.map((choice, idx) => {
      const owned = draftOwnedLevel(choice);
      const rarityLabel = (choice.rarity || "rare").toUpperCase();
      const canAfford = armory.credits >= choice.cost;

      return `
        <div class="relic-card" data-rarity="${choice.rarity}">
          <div class="relic-rarity">${rarityLabel}</div>
          <div class="relic-name">${choice.name}</div>
          <div class="relic-desc">${choice.desc}</div>
          <div class="relic-tagline">${draftTagline(choice)}${owned > 0 && !choice.oneShot ? ` • Owned ${owned}` : ""}</div>
          <div class="relic-buy">
            <span class="relic-cost">${fmt(choice.cost)} salvage</span>
            <button class="relic-btn" type="button" data-buy="${idx}" ${canAfford ? "" : "data-poor='1'"}>
              ${canAfford ? "Koop" : "Te duur"}
            </button>
          </div>
        </div>
      `;
    }).join("");

    armory.draft.cards.querySelectorAll("[data-buy]").forEach(btn => {
      btn.addEventListener("click", () => buyRelic(Number(btn.dataset.buy)));
    });

    armory.draft.rerollBtn.disabled = armory.rerolls <= 0;
    armory.draft.rerollBtn.textContent = armory.rerolls > 0
      ? `Reroll (${armory.rerolls})`
      : "Reroll op";
  }

  function rerollDraft(){
    if(armory.rerolls <= 0 || !armory.draftOpen) return;
    armory.rerolls -= 1;
    generateDraftChoices();
    updateHud();
    flashHint?.("Nieuwe relics binnen", 850);
  }

  function buyRelic(idx){
    const choice = armory.currentChoices?.[idx];
    if(!choice) return;

    if(armory.credits < choice.cost){
      flashHint?.("Niet genoeg salvage", 900);
      return;
    }

    armory.credits -= choice.cost;
    armory.relicsTaken += 1;
    armory.profile.totalRelics += 1;

    choice.apply?.();

    if(choice.needsSync){
      syncDerivedStats();
    }

    saveProfile();
    updateHud();
    flashHint?.(`Relic gekocht: ${choice.name}`, 1200);

    if(!choice.oneShot){
      draftResupply(1);
    } else {
      player.hp = Math.min(player.maxHp, player.hp + 8);
      setStat?.();
    }

    closeDraftAndContinue();
  }

  function shouldOfferDraft(nextWave){
    if(nextWave <= 1) return false;
    if(nextWave % 5 === 0) return true;
    if(nextWave % 3 === 0) return true;
    return false;
  }

  function openDraft(nextWave, continueFn){
    armory.draftOpen = true;
    armory.awaitingWaveStart = true;
    armory.continueFn = continueFn;

    state.running = false;
    state.fireHeld = false;
    if(document.pointerLockElement === renderer.domElement){
      document.exitPointerLock?.();
    }

    armory.draft.root.classList.add("show");
    armory.draft.title.textContent = nextWave % 5 === 0
      ? `Major Relic Draft • Wave ${nextWave}`
      : `Relic Draft • Wave ${nextWave}`;

    armory.draft.sub.textContent = nextWave % 5 === 0
      ? `Boss-wave in aantocht. Je hebt ${fmt(armory.credits)} salvage — kies een duidelijke upgrade voor schade, overleving of crowd control.`
      : `Je hebt ${fmt(armory.credits)} salvage — kies een upgrade die past bij je build: schade, mobiliteit, sustain, abilities of economie.`;

    generateDraftChoices();
    updateHud();
  }

  function closeDraftAndContinue(){
    armory.draftOpen = false;
    armory.awaitingWaveStart = false;
    armory.draft.root.classList.remove("show");

    const fn = armory.continueFn;
    armory.continueFn = null;
    updateHud();

    if(typeof fn === "function"){
      fn();
    }

    if(!isTouch && state.running){
      setTimeout(() => renderer.domElement.requestPointerLock?.(), 60);
    }
  }

  function resetRun(){
    armory.credits = 0;
    armory.earnedThisRun = 0;
    armory.relicsTaken = 0;
    armory.rerolls = 1;
    armory.awaitingWaveStart = false;
    armory.draftOpen = false;
    armory.continueFn = null;
    armory.reviveUsed = false;
    Object.keys(armory.levels).forEach(k => armory.levels[k] = 0);
    syncDerivedStats();
    updateHud();
  }

  function finishRunSnapshot(){
    armory.profile.runs += 1;
    armory.profile.bestWave = Math.max(armory.profile.bestWave || 1, player.wave || 1);
    armory.profile.bestScore = Math.max(armory.profile.bestScore || 0, Math.floor(player.score || 0));
    saveProfile();
    updateHud();
  }

  /* ---------- wrappers ---------- */

  const _createProjectile = createProjectile;
  createProjectile = function(start, dir, opts = {}){
    const copy = { ...opts };

    if(copy.friendly){
      const dmgMul = 1 + armory.levels.overclock * 0.18;
      const speedMul = 1 + armory.levels.rapidfire * 0.03;
      const radiusMul = 1 + armory.levels.capacitor * 0.12;

      copy.damage = (copy.damage || 0) * dmgMul;
      copy.speed = (copy.speed || 0) * speedMul;

      if(copy.type === "rocket" || copy.type === "grenade" || copy.type === "plasma"){
        copy.radius = (copy.radius || 0) * radiusMul;
      }
    }

    return _createProjectile(start, dir, copy);
  };

  const _shootWithDirection = shootWithDirection;
  shootWithDirection = function(dirOverride = null){
    if(armory.levels.rapidfire > 0){
      player.fireCooldown *= Math.max(0.7, 1 - armory.levels.rapidfire * 0.06);
    }

    const ok = _shootWithDirection(dirOverride);

    if(ok && armory.levels.echo > 0 && player.weapon === "bullet"){
      const chance = 0.15 + armory.levels.echo * 0.08;
      if(Math.random() < chance){
        const dir = dirOverride ? dirOverride.clone() : new THREE.Vector3();
        if(!dirOverride){
          camera.getWorldDirection(dir);
        }
        dir.normalize();

        const right = new THREE.Vector3(dir.z, 0, -dir.x).normalize();
        const a = dir.clone().addScaledVector(right, 0.16).normalize();
        const b = dir.clone().addScaledVector(right, -0.16).normalize();
        const base = player.pos.clone();
        base.y = 1.52;

        [a, b].forEach((d, idx) => {
          const start = base.clone()
            .addScaledVector(right, idx === 0 ? 0.20 : -0.20)
            .add(d.clone().multiplyScalar(.82));

          state.bullets.push(createProjectile(start, d, {
            speed: 30,
            friendly: true,
            color: idx === 0 ? 0xffd166 : 0x8bf0ff,
            trailColor: 0xffffff,
            size: 0.08,
            life: 1.08,
            damage: 5 + armory.levels.echo * 1.5,
            type: "echo"
          }));
        });

        createFlash?.(base.clone(), 0xffd166, 0.9, 2.4, 0.05);
      }
    }

    return ok;
  };

  const _registerKill = registerKill;
  registerKill = function(points){
    _registerKill(points);

    if(armory.levels.combo > 0){
      state.comboTimer += armory.levels.combo * 0.45;
      state.combo = clamp(state.combo + armory.levels.combo * 0.03, 1, 3.6 + armory.levels.combo * 0.3);
      player.score += Math.round(points * 0.08 * armory.levels.combo);
    }

    if(armory.levels.vamp > 0){
      player.hp = Math.min(player.maxHp, player.hp + armory.levels.vamp * 2);
    }

    setStat?.();
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    if(enemy?.isBoss){
      awardCredits(28, "boss");
    }else if(enemy?.type === "elite"){
      awardCredits(10, "elite");
    }else if(enemy?.type === "tank"){
      awardCredits(8, "tank");
    }else if(enemy?.type === "runner"){
      awardCredits(6, "runner");
    }else{
      awardCredits(5, "kill");
    }
    return _killEnemy(enemy);
  };

  const _applyDamage = applyDamage;
  applyDamage = function(amount){
    let finalAmount = amount * (1 - armory.levels.plating * 0.14);

    if(
      armory.levels.phoenix > 0 &&
      !armory.reviveUsed &&
      player.alive &&
      player.hp - finalAmount <= 0
    ){
      armory.reviveUsed = true;
      player.hp = Math.max(45, Math.floor(player.maxHp * 0.45));
      player.damageCooldown = 1.25;
      state.cameraShake = Math.min(2.2, state.cameraShake + 1.0);
      createShockwave?.(player.pos.clone(), 0xffd166, 4.4);
      createShockwave?.(player.pos.clone(), 0xff4fd8, 3.2);
      flashHint?.("PHOENIX PROTOCOL", 1600);
      setStat?.();
      updateHud();
      return;
    }

    const result = _applyDamage(finalAmount);

    if(!player.alive){
      finishRunSnapshot();
    }

    return result;
  };

  const _restartGame = restartGame;
  restartGame = function(){
    finishRunSnapshot();
    resetRun();
    return _restartGame();
  };

  queueNextWave = function(delay = 1.2){
    if(state.nextWaveQueued || !player.alive) return;

    state.nextWaveQueued = true;

    setTimeout(() => {
      state.nextWaveQueued = false;
      if(!player.alive) return;
      if(!(state.running && state.enemies.length === 0 && !state.boss)) return;

      const nextWave = player.wave + 1;

      const startNextWave = () => {
        player.wave = nextWave;
        state.moonNukeWave = 0;
        player.hp = Math.min(player.maxHp, player.hp + 12 + armory.levels.frame * 2);
        player.ammo.bullet += 6 + armory.levels.printer * 4;
        if(armory.levels.printer > 0) player.ammo.rocket += 1;
        if(armory.levels.printer > 1) player.ammo.grenade += 1;
        if(armory.levels.capacitor > 1 && nextWave % 3 === 0){
          player.abilities.plasma += 1;
          player.abilities.mine += 1;
        }

        awardCredits(10 + Math.floor(nextWave * 1.5), `wave ${nextWave}`);
        state.lastClearStamp = performance.now();
        state.running = true;
        setStat?.();
        updateHud();
        spawnWave();
      };

      if(shouldOfferDraft(nextWave)){
        openDraft(nextWave, startNextWave);
      } else {
        startNextWave();
      }
    }, delay * 1000);
  };

  const _startGame = startGame;
  startGame = function(){
    armory.profile.runs = armory.profile.runs || 0;
    updateHud();
    return _startGame();
  };

  /* ---------- hotkeys ---------- */
  function onArmoryHotkeys(e){
    const code = e.code || "";
    if(code === "Tab"){
      if(!armory.awaitingWaveStart && !armory.draftOpen) return;
      e.preventDefault();
      if(armory.draftOpen){
        closeDraftAndContinue();
      }
    }
    if(code === "KeyB"){
      if(armory.awaitingWaveStart && armory.draftOpen){
        e.preventDefault();
        closeDraftAndContinue();
      }
    }
  }
  window.addEventListener("keydown", onArmoryHotkeys, { capture:true, passive:false });

  /* ---------- init ---------- */
  buildHud();
  resetRun();
  syncDerivedStats();
})();


/* ===========================
   OH NAMED ENEMY SKILLS PACK
   plak dit HELE blok onderin je game-code
   =========================== */
(() => {
  const OH_NAMED_ENEMIES = ["Pieter","Jos","Lisa","Joost","Jan","Patrick","Miranda","Jad","Kevin","Elly","Mark"];

  const NAMED_TRAITS = {
    Pieter: {
      role: "captain",
      hpMul: 1.45,
      speedMul: 1.02,
      color: 0xffd166,
      label: "Captain",
      onSpawn(enemy){
        enemy.fireRateMul = 0.82;
        enemy.damageMul = 1.18;
      }
    },
    Jos: {
      role: "turret",
      hpMul: 1.9,
      speedMul: 0.0,
      color: 0xff965e,
      label: "Turret",
      onSpawn(enemy){
        enemy.fireRateMul = 0.62;
        enemy.damageMul = 2.15;
        enemy.isStaticNamed = true;
      }
    },
    Lisa: {
      role: "bounty",
      hpMul: 0.96,
      speedMul: 1.12,
      color: 0xff87d1,
      label: "Bounty",
      onSpawn(enemy){
        enemy.fireRateMul = 0.90;
        enemy.damageMul = 1.00;
      },
onHit(enemy, damage){
  const bonus = Math.max(12, Math.round(damage * 2.2));
  player.score += bonus;

  state.comboTimer = Math.max(state.comboTimer, 1.2);

  showFloating(`LISA BONUS +${bonus}`);

  // groene cash explosie
  const origin = enemy.mesh.position.clone();
  origin.y += 1.4;

  for(let i = 0; i < 14; i++){

    const bill = new THREE.Mesh(
      new THREE.PlaneGeometry(0.22, 0.12),
      new THREE.MeshBasicMaterial({
        color: 0x4cff7a,
        transparent: true,
        opacity: 0.9,
        side: THREE.DoubleSide
      })
    );

    bill.position.copy(origin);
    bill.rotation.set(rand(-0.4,0.4), rand(0,Math.PI), rand(-0.4,0.4));

    scene.add(bill);

    state.particles.push({
      mesh: bill,
      vel: new THREE.Vector3(
        rand(-2.2,2.2),
        rand(2.0,4.2),
        rand(-2.2,2.2)
      ),
      life: rand(0.6,1.2),
      drag: 0.92,
      gravity: -3.5,
      rotate: rand(-6,6),
      shrink: 0.98
    });

  }

  createFlash?.(
    enemy.mesh.position.clone().add(new THREE.Vector3(0,1.4,0)),
    0x4cff7a,
    1.3,
    3.6,
    0.06
  );

  setStat?.();
}

    },
    Joost: {
      role: "skirmisher",
      hpMul: 0.92,
      speedMul: 1.26,
      color: 0x8ff6ff,
      label: "Skirmisher",
      onSpawn(enemy){
        enemy.fireRateMul = 0.92;
        enemy.damageMul = 0.96;
      }
    },
    Jan: {
      role: "bruiser",
      hpMul: 1.34,
      speedMul: 0.92,
      color: 0xffc45f,
      label: "Bruiser",
      onSpawn(enemy){
        enemy.fireRateMul = 1.05;
        enemy.damageMul = 1.15;
      }
    },
    Patrick: {
      role: "grenadier",
      hpMul: 1.08,
      speedMul: 1.06,
      color: 0x7affb7,
      label: "Grenadier",
      onSpawn(enemy){
        enemy.fireRateMul = 1.12;
        enemy.damageMul = 1.0;
        enemy.alwaysFlee = false;
        enemy.neverShoot = false;
        enemy.usesGrenades = true;
        enemy.preferredDistance = 2.4;
      }
    },
    Elly: {
      role: "halo runner",
      hpMul: 0.9,
      speedMul: 1.34,
      color: 0xff7bff,
      label: "Halo Runner",
      onSpawn(enemy){
        enemy.fireRateMul = 0.88;
        enemy.damageMul = 0.9;
        enemy.preferredDistance = 3.2;
      }
    },
    Mark: {
      role: "demolisher",
      hpMul: 1.62,
      speedMul: 0.82,
      color: 0xff8a5b,
      label: "Demolisher",
      onSpawn(enemy){
        enemy.fireRateMul = 1.16;
        enemy.damageMul = 1.22;
        enemy.preferredDistance = 2.1;
      }
    },
    Miranda: {
      role: "adrenaline",
      hpMul: 1.02,
      speedMul: 1.06,
      color: 0x8eb8ff,
      label: "Adrenaline",
      onSpawn(enemy){
        enemy.fireRateMul = 0.95;
        enemy.damageMul = 1.00;
      },
      onHit(enemy){
        const now = performance.now() * 0.001;
        player._ohSpeedBoostUntil = Math.max(player._ohSpeedBoostUntil || 0, now + 2.5);
        showFloating("MIRANDA BOOST");
        createShockwave?.(player.pos.clone(), 0x8eb8ff, 2.2);
      }
    },
    Jad: {
      role: "glitch",
      hpMul: 1.00,
      speedMul: 1.04,
      color: 0x9eff84,
      label: "Glitch",
      onSpawn(enemy){
        enemy.fireRateMul = 0.88;
        enemy.damageMul = 0.92;
        enemy.wrongAim = true;
      }
    },
    Kevin: {
      role: "marksman",
      hpMul: 1.10,
      speedMul: 1.00,
      color: 0xaadfff,
      label: "Marksman",
      onSpawn(enemy){
        enemy.fireRateMul = 0.84;
        enemy.damageMul = 1.12;
      }
    }
  };

  function ohPickName(){
    return OH_NAMED_ENEMIES[Math.floor(Math.random() * OH_NAMED_ENEMIES.length)];
  }

  function ohEnsureTagText(enemy){
    const sprite = enemy?.mesh?.userData?.parts?.nameTag;
    if(!sprite?.material?.map?.image) return;

    const canvas = sprite.material.map.image;
    const ctx = canvas.getContext?.("2d");
    if(!ctx) return;

    const name = enemy.enemyName || "OH Unit";
    const color = NAMED_TRAITS[name]?.color || 0x00f7ff;
    const glow = "#" + color.toString(16).padStart(6, "0");
    const role = NAMED_TRAITS[name]?.label || "OH";

    ctx.clearRect(0, 0, canvas.width, canvas.height);

    ctx.fillStyle = "rgba(6,10,22,.86)";
    ctx.strokeStyle = "rgba(255,255,255,.18)";
    ctx.lineWidth = 3;
    if(ctx.roundRect){
      ctx.beginPath();
      ctx.roundRect(8, 10, canvas.width - 16, 62, 18);
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(8, 10, canvas.width - 16, 62);
      ctx.strokeRect(8, 10, canvas.width - 16, 62);
    }

    ctx.shadowColor = glow;
    ctx.shadowBlur = 18;
    ctx.fillStyle = "#f7fbff";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.font = "900 27px system-ui";
    ctx.fillText(name, canvas.width * 0.5, 38);

    ctx.shadowBlur = 0;
    ctx.fillStyle = "rgba(255,255,255,.70)";
    ctx.font = "700 15px system-ui";
    ctx.fillText(role, canvas.width * 0.5, 61);

    sprite.material.map.needsUpdate = true;
  }

  function ohDecorateNamedEnemy(enemy){
    if(!enemy || enemy.isBoss || enemy.__ohNamedApplied) return enemy;

    let name = enemy.enemyName;
    if(!OH_NAMED_ENEMIES.includes(name)){
      name = ohPickName();
    }

    const trait = NAMED_TRAITS[name];
    enemy.enemyName = name;
    enemy.namedTrait = trait?.role || "oh";
    enemy.__ohNamedApplied = true;

    enemy.maxHp = Math.round(enemy.maxHp * (trait?.hpMul || 1));
    enemy.hp = Math.round(enemy.hp * (trait?.hpMul || 1));
    enemy.speed *= trait?.speedMul ?? 1;
    enemy.fireRateMul = trait?.fireRateMul || 1;
    enemy.damageMul = trait?.damageMul || 1;

    if(enemy.mesh){
      enemy.mesh.userData.enemyName = name;
      enemy.mesh.userData.namedTrait = trait?.role || "oh";
    }

    if(enemy.groundRing?.material && trait?.color){
      enemy.groundRing.material.color.setHex(trait.color);
      enemy.groundRing.material.opacity = Math.max(enemy.groundRing.material.opacity || 0.22, 0.28);
    }

    if(enemy.mesh?.userData?.parts?.logo?.material && trait?.color){
      enemy.mesh.userData.parts.logo.material.emissive?.setHex?.(trait.color);
      enemy.mesh.userData.parts.logo.material.emissiveIntensity = 0.34;
    }

    if(enemy.mesh?.userData?.parts?.backLogo?.material && trait?.color){
      enemy.mesh.userData.parts.backLogo.material.emissive?.setHex?.(trait.color);
      enemy.mesh.userData.parts.backLogo.material.emissiveIntensity = 0.26;
    }

    if(enemy.mesh?.userData?.parts?.nameTag){
      ohEnsureTagText(enemy);
    }

    if(typeof trait?.onSpawn === "function"){
      trait.onSpawn(enemy);
    }

    return enemy;
  }

  function ohPieterDownSting(){
    if(typeof audioCtx === "undefined" || !audioCtx) return;
    const t = audioCtx.currentTime + 0.01;

    const notes = [392, 523.25, 659.25, 783.99];
    notes.forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      osc.type = i < 2 ? "sawtooth" : "triangle";
      osc.frequency.setValueAtTime(freq, t + i * 0.08);
      gain.gain.setValueAtTime(0.0001, t + i * 0.08);
      gain.gain.exponentialRampToValueAtTime(0.05, t + i * 0.08 + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.08 + 0.22);
      osc.connect(gain).connect(audioCtx.destination);
      osc.start(t + i * 0.08);
      osc.stop(t + i * 0.08 + 0.24);
    });
  }

  function ohNamedOnHit(enemy, damage){
    if(!enemy?.enemyName) return;
    const trait = NAMED_TRAITS[enemy.enemyName];
    if(typeof trait?.onHit === "function"){
      trait.onHit(enemy, damage);
    }
  }

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss = false){
    const beforeCount = state.enemies.length;
    const hadBoss = !!state.boss;

    const result = _spawnEnemy(isBoss);

    let enemy = null;
    if(isBoss){
      enemy = state.boss;
    } else if(state.enemies.length > beforeCount){
      enemy = state.enemies[state.enemies.length - 1];
    } else if(!hadBoss && state.boss && isBoss){
      enemy = state.boss;
    }

    if(enemy && !enemy.isBoss){
      ohDecorateNamedEnemy(enemy);
    }

    return result;
  };

  const _enemyShoot = enemyShoot;
  enemyShoot = function(enemy){
    if(!enemy) return;

    const name = enemy.enemyName || "";
    const start = enemy.mesh.position.clone();
    start.y = enemy.isBoss ? 2.9 : 2.05;

    if(name === "Patrick" || name === "Mark"){
      const target = player.pos.clone();
      target.y = 1.1;
      const toTarget = target.sub(start);
      const flatDist = Math.hypot(toTarget.x, toTarget.z) || 1;
      const dir = toTarget.normalize();
      const arcBase = name === "Mark" ? 0.3 : 0.24;
      dir.y = clamp(arcBase + flatDist * 0.012, arcBase, name === "Mark" ? 0.52 : 0.48);
      dir.x += rand(-0.025, 0.025);
      dir.z += rand(-0.025, 0.025);
      dir.normalize();

      state.ohEnemyGrenades = state.ohEnemyGrenades || [];
      const grenade = createProjectile(start.clone(), dir, {
        speed: name === "Mark" ? 9.6 : 10.5,
        friendly: false,
        color: name === "Mark" ? 0xff8a5b : 0x9dff7c,
        trailColor: name === "Mark" ? 0xffc29b : 0xd8ffca,
        size: name === "Mark" ? 0.19 : 0.15,
        life: name === "Mark" ? 1.85 : 1.55,
        damage: name === "Mark" ? 24 : 18,
        radius: name === "Mark" ? 3.4 : 2.7,
        type: "enemy_grenade",
        gravity: 9.6,
        explosionColor: name === "Mark" ? 0xff8a5b : 0x9dff7c
      });
      grenade.enemyGrenade = true;
      grenade.spin = rand(7, 12);
      grenade.trailEvery = name === "Mark" ? 0.07 : 0.05;
      grenade.trailClock = 0;
      state.ohEnemyGrenades.push(grenade);

      createFlash?.(start.clone(), name === "Mark" ? 0xff8a5b : 0x9dff7c, name === "Mark" ? 0.9 : 0.7, name === "Mark" ? 2.8 : 2.2, 0.04);
      return;
    }

    if(name === "Elly"){
      const target = player.pos.clone();
      target.y = 1.35;
      const dir = target.sub(start).normalize();
      for(const yaw of [-0.08, 0.08]){
        const shotDir = dir.clone();
        shotDir.x += yaw;
        shotDir.y += rand(-0.012, 0.02);
        shotDir.z += rand(-0.03, 0.03);
        shotDir.normalize();
        state.enemyBullets.push(createProjectile(start.clone(), shotDir, {
          speed: 14.5,
          friendly: false,
          color: 0xff7bff,
          trailColor: 0xcafcff,
          size: 0.11,
          life: 2.8,
          damage: 10,
          type: "enemy"
        }));
      }
      createFlash?.(start.clone(), 0xff7bff, 0.82, 2.7, 0.04);
      return;
    }

    if(enemy.neverShoot){
      return;
    }

    if(name === "Jos"){
      const target = player.pos.clone();
      target.y = 1.45;
      const dir = target.sub(start).normalize();

      for(let i = 0; i < 3; i++){
        const shotDir = dir.clone();
        shotDir.x += (i - 1) * 0.08;
        shotDir.y += (Math.random() - 0.5) * 0.015;
        shotDir.z += (Math.random() - 0.5) * 0.02;
        shotDir.normalize();

        state.enemyBullets.push(createProjectile(start.clone(), shotDir, {
          speed: 11,
          friendly: false,
          color: 0xff965e,
          size: 0.19,
          life: 3.6,
          damage: 24,
          type: "enemy"
        }));
      }

      createFlash?.(start.clone(), 0xff965e, 1.0, 4.6, 0.07);
      return;
    }

    if(name === "Jad" || enemy.wrongAim){
      const target = player.pos.clone();
      target.y = 1.45;
      const dir = target.sub(start).normalize();

      const wrongYaw = (Math.random() < 0.5 ? 1 : -1) * (Math.PI * (0.55 + Math.random() * 0.35));
      const sin = Math.sin(wrongYaw);
      const cos = Math.cos(wrongYaw);

      const badDir = new THREE.Vector3(
        dir.x * cos - dir.z * sin,
        clamp(dir.y + rand(-0.18, 0.18), -0.35, 0.35),
        dir.x * sin + dir.z * cos
      ).normalize();

      state.enemyBullets.push(createProjectile(start.clone(), badDir, {
        speed: 13,
        friendly: false,
        color: 0x9eff84,
        size: 0.13,
        life: 3.2,
        damage: 9,
        type: "enemy"
      }));

      createFlash?.(start.clone(), 0x9eff84, 0.8, 2.6, 0.04);
      return;
    }

    const oldCooldown = enemy.fireCooldown;
    if(enemy.fireRateMul && enemy.fireRateMul !== 1){
      enemy.fireCooldown = oldCooldown * enemy.fireRateMul;
    }

    _enemyShoot(enemy);

    if(enemy.damageMul && enemy.damageMul !== 1){
      const last = state.enemyBullets[state.enemyBullets.length - 1];
      if(last && !last.friendly){
        last.damage *= enemy.damageMul;
        if(name === "Kevin"){
          last.vel.multiplyScalar(1.12);
          last.mesh.scale.setScalar(1.08);
        }
      }
      if(name === "Pieter" && state.enemyBullets.length >= 2){
        for(let i = state.enemyBullets.length - 2; i < state.enemyBullets.length; i++){
          const b = state.enemyBullets[i];
          if(b && !b.friendly){
            b.damage *= 1.12;
            b.vel.multiplyScalar(1.04);
          }
        }
      }
    }

    enemy.fireCooldown = oldCooldown;
  };

  const _updateEnemies = updateEnemies;
  updateEnemies = function(dt){
    _updateEnemies(dt);

    for(const enemy of state.enemies){
      if(!enemy?.mesh) continue;

      const name = enemy.enemyName || "";

      if(name === "Jos"){
        if(!enemy._staticAnchor){
          enemy._staticAnchor = enemy.mesh.position.clone();
        }
        enemy.mesh.position.x = enemy._staticAnchor.x;
        enemy.mesh.position.z = enemy._staticAnchor.z;
        if(enemy.groundRing){
          enemy.groundRing.position.set(enemy.mesh.position.x, 0.03, enemy.mesh.position.z);
        }
      }

      if(enemy.alwaysFlee){
        const dx = enemy.mesh.position.x - player.pos.x;
        const dz = enemy.mesh.position.z - player.pos.z;
        const dist = Math.hypot(dx, dz) || 1;
        const awayX = dx / dist;
        const awayZ = dz / dist;

        const fleeStep = (enemy.speed || 4) * 0.62 * dt;
        const nx = enemy.mesh.position.x + awayX * fleeStep;
        const nz = enemy.mesh.position.z + awayZ * fleeStep;

        if(!collidesAt(nx, nz, enemy.radius || 1)){
          enemy.mesh.position.x = nx;
          enemy.mesh.position.z = nz;
        }

        enemy.mesh.lookAt(
          enemy.mesh.position.x + awayX * 3,
          1.6,
          enemy.mesh.position.z + awayZ * 3
        );

        if(enemy.groundRing){
          enemy.groundRing.position.set(enemy.mesh.position.x, 0.03, enemy.mesh.position.z);
        }
      }

      if(name === "Joost"){
        enemy.mesh.position.y += Math.sin(performance.now() * 0.012 + enemy.bob) * 0.003;
      }

      if(name === "Kevin" && enemy.groundRing){
        enemy.groundRing.rotation.z += dt * 1.8;
      }
    }
  };

  const _updateMovement = updateMovement;
  updateMovement = function(dt){
    if(player._ohBaseSpeed == null) player._ohBaseSpeed = player.speed;

    const now = performance.now() * 0.001;
    const bonusActive = (player._ohSpeedBoostUntil || 0) > now;
    const relicMult = 1 + (player.speedBoost || 0);
    player.speed = (player._ohBaseSpeed + (bonusActive ? 3.2 : 0)) * relicMult;

    _updateMovement(dt);

    player.speed = player._ohBaseSpeed;
  };

  const _damageEnemyDirect = typeof damageEnemyDirect === "function" ? damageEnemyDirect : null;
  if(_damageEnemyDirect){
    damageEnemyDirect = function(enemy, damage){
      const before = enemy?.hp;
      const ok = _damageEnemyDirect(enemy, damage);
      if(ok && enemy && typeof before === "number" && enemy.hp < before){
        ohNamedOnHit(enemy, before - enemy.hp);
      }
      return ok;
    };
  }

  const _updateBullets = updateBullets;
  updateBullets = function(dt){
    const hpBefore = new Map();
    state.enemies.forEach(e => hpBefore.set(e, e.hp));
    const bossRef = state.boss;
    const bossHpBefore = bossRef ? bossRef.hp : null;

    _updateBullets(dt);

    for(const [enemy, before] of hpBefore.entries()){
      if(typeof before === "number" && enemy && enemy.hp < before){
        ohNamedOnHit(enemy, before - enemy.hp);
      }
    }

    if(bossRef && typeof bossHpBefore === "number" && bossRef.hp < bossHpBefore){
      ohNamedOnHit(bossRef, bossHpBefore - bossRef.hp);
    }
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    if(enemy?.enemyName === "Pieter"){
      ohPieterDownSting();
      showFloating("PIETER DOWN");
      createShockwave?.(enemy.mesh.position.clone(), 0xffd166, 3.6);
    }
    return _killEnemy(enemy);
  };

  const _restartGame = restartGame;
  restartGame = function(){
    player._ohSpeedBoostUntil = 0;
    player._ohBaseSpeed = player.speed || 10.2;
    return _restartGame();
  };

  // bestaande enemies direct updaten als je tijdens een lopende run plakt
  for(const enemy of state.enemies){
    ohDecorateNamedEnemy(enemy);
  }

  showFloating("OH named skills online");
})();

/* ==========================================
   OH NAMED ENEMY EXTRA PATCH
   Patrick vlucht echt weg
   Jan schiet boeken
   Kevin zwart pak
   Pieter death = sting + 3s freeze
   Plak DIT HELE BLOK onderaan je code
   ========================================== */
(() => {
  function ohFindNamedEnemy(enemy){
    return enemy?.enemyName || enemy?.mesh?.userData?.enemyName || "";
  }

  function ohAddFreezeOverlay(){
    if(document.getElementById("ohFreezeOverlay")) return;

    const overlay = document.createElement("div");
    overlay.id = "ohFreezeOverlay";
    overlay.style.cssText = `
      position:fixed;
      inset:0;
      display:none;
      align-items:center;
      justify-content:center;
      background:radial-gradient(circle at 50% 40%, rgba(255,210,120,.10), rgba(0,0,0,.56));
      backdrop-filter: blur(3px);
      z-index:9999;
      pointer-events:none;
      font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
      letter-spacing:.08em;
      text-transform:uppercase;
      color:#fff5d2;
      font-weight:900;
      font-size:min(5vw,32px);
      text-shadow:0 0 24px rgba(255,209,102,.25);
    `;
    overlay.textContent = "PIETER DOWN";
    document.body.appendChild(overlay);
  }

  function ohSetFreeze(active){
    ohAddFreezeOverlay();
    const overlay = document.getElementById("ohFreezeOverlay");
    if(overlay) overlay.style.display = active ? "flex" : "none";
  }

  function ohFreezeGame(seconds = 3){
    state.ohFreezeUntil = performance.now() + seconds * 1000;
    ohSetFreeze(true);
    flashHint?.(`Pieter down — ${seconds} sec pauze`, seconds * 1000);
    createShockwave?.(player.pos.clone(), 0xffd166, 2.8);
  }

  function ohFreezeActive(){
    const active = (state.ohFreezeUntil || 0) > performance.now();
    if(!active) ohSetFreeze(false);
    return active;
  }

  function ohPieterSting(){
    if(typeof audioCtx === "undefined" || !audioCtx) return;

    const t = audioCtx.currentTime + 0.01;
    const notes = [523.25, 659.25, 783.99, 1046.5];

    notes.forEach((freq, i) => {
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();
      const filter = audioCtx.createBiquadFilter();

      osc.type = i < 2 ? "sawtooth" : "triangle";
      osc.frequency.setValueAtTime(freq, t + i * 0.09);

      filter.type = "lowpass";
      filter.frequency.setValueAtTime(1800, t + i * 0.09);

      gain.gain.setValueAtTime(0.0001, t + i * 0.09);
      gain.gain.exponentialRampToValueAtTime(0.06, t + i * 0.09 + 0.018);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + i * 0.09 + 0.26);

      osc.connect(filter);
      filter.connect(gain);
      gain.connect(audioCtx.destination);

      osc.start(t + i * 0.09);
      osc.stop(t + i * 0.09 + 0.28);
    });
  }

  function ohMakeBookProjectile(start, dir){
    const proj = createProjectile(start, dir, {
      speed: 11.5,
      friendly: false,
      color: 0x6f4328,
      size: 0.16,
      life: 3.2,
      damage: 14,
      type: "enemy_book"
    });

    if(proj?.mesh){
      scene.remove(proj.mesh);

      const book = new THREE.Group();

      const cover = new THREE.Mesh(
        new THREE.BoxGeometry(0.34, 0.10, 0.46),
        new THREE.MeshStandardMaterial({
          color: 0x7a3d18,
          roughness: 0.82,
          metalness: 0.04
        })
      );

      const pages = new THREE.Mesh(
        new THREE.BoxGeometry(0.28, 0.07, 0.40),
        new THREE.MeshStandardMaterial({
          color: 0xe8dfc8,
          roughness: 0.95,
          metalness: 0.0
        })
      );
      pages.position.y = 0.01;

      const spine = new THREE.Mesh(
        new THREE.BoxGeometry(0.05, 0.11, 0.46),
        new THREE.MeshStandardMaterial({
          color: 0x3a1f12,
          roughness: 0.8,
          metalness: 0.05
        })
      );
      spine.position.x = -0.145;

      const title = new THREE.Mesh(
        new THREE.BoxGeometry(0.12, 0.012, 0.20),
        new THREE.MeshStandardMaterial({
          color: 0xffd166,
          emissive: 0x7a5e10,
          emissiveIntensity: 0.24,
          roughness: 0.4,
          metalness: 0.15
        })
      );
      title.position.set(0.03, 0.056, 0.00);

      book.add(cover, pages, spine, title);
      book.position.copy(start);
      book.rotation.set(rand(-0.5, 0.5), rand(-0.5, 0.5), rand(-0.5, 0.5));
      scene.add(book);

      proj.mesh = book;
      proj.spin = rand(9, 16);
      proj.damage = 18;
      proj.trailColor = 0xffd166;
      proj.explosionColor = 0xffd166;
    }

    return proj;
  }

  function ohStyleKevin(enemy){
    if(!enemy?.mesh || enemy.__ohKevinStyled) return;
    if(ohFindNamedEnemy(enemy) !== "Kevin") return;

    const parts = enemy.mesh.userData?.parts || {};

    const darkSuit = new THREE.Color(0x101114);
    const darker = new THREE.Color(0x07080a);
    const trim = new THREE.Color(0xd7dde7);

    const recolorMat = (mat, color, emissive = null, emissiveIntensity = null) => {
      if(!mat) return;
      if(mat.color) mat.color.copy(color);
      if(emissive && mat.emissive) mat.emissive.copy(emissive);
      if(emissiveIntensity != null) mat.emissiveIntensity = emissiveIntensity;
      mat.needsUpdate = true;
    };

    recolorMat(parts.torso?.material, darkSuit, darker, 0.18);
    recolorMat(parts.pelvis?.material, darker, darker, 0.10);
    recolorMat(parts.armL?.material, darkSuit, darker, 0.12);
    recolorMat(parts.armR?.material, darkSuit, darker, 0.12);
    recolorMat(parts.foreArmL?.material, darker, darker, 0.08);
    recolorMat(parts.foreArmR?.material, darker, darker, 0.08);
    recolorMat(parts.legL?.material, darker, darker, 0.08);
    recolorMat(parts.legR?.material, darker, darker, 0.08);
    recolorMat(parts.shinL?.material, darkSuit, darker, 0.08);
    recolorMat(parts.shinR?.material, darkSuit, darker, 0.08);
    recolorMat(parts.bootL?.material, darker, darker, 0.08);
    recolorMat(parts.bootR?.material, darker, darker, 0.08);
    recolorMat(parts.chest?.material, trim, new THREE.Color(0x3e4650), 0.12);
    recolorMat(parts.cape?.material, new THREE.Color(0x111214), new THREE.Color(0x08090b), 0.06);
    recolorMat(parts.gauntletL?.material, trim, new THREE.Color(0x3e4650), 0.22);
    recolorMat(parts.gauntletR?.material, trim, new THREE.Color(0x3e4650), 0.22);

    if(parts.logo?.traverse){
      parts.logo.traverse(obj => {
        if(obj.material?.color) obj.material.color.setHex(0xf2f6ff);
        if(obj.material?.emissive) obj.material.emissive.setHex(0x8d98a8);
        if(obj.material) obj.material.emissiveIntensity = 0.28;
      });
    }
    if(parts.backLogo?.traverse){
      parts.backLogo.traverse(obj => {
        if(obj.material?.color) obj.material.color.setHex(0xf2f6ff);
        if(obj.material?.emissive) obj.material.emissive.setHex(0x8d98a8);
        if(obj.material) obj.material.emissiveIntensity = 0.22;
      });
    }

    if(enemy.groundRing?.material){
      enemy.groundRing.material.color.setHex(0xc7d0dc);
      enemy.groundRing.material.opacity = 0.24;
    }

    enemy.__ohKevinStyled = true;
  }

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss = false){
    const before = state.enemies.length;
    const hadBoss = !!state.boss;

    const result = _spawnEnemy(isBoss);

    let enemy = null;
    if(isBoss){
      enemy = state.boss;
    } else if(state.enemies.length > before){
      enemy = state.enemies[state.enemies.length - 1];
    } else if(!hadBoss && isBoss && state.boss){
      enemy = state.boss;
    }

    if(enemy){
      ohStyleKevin(enemy);
    }

    return result;
  };

  const _enemyShoot = enemyShoot;
  enemyShoot = function(enemy){
    const name = ohFindNamedEnemy(enemy);

    if(name === "Jan"){
      const start = enemy.mesh.position.clone();
      start.y = enemy.isBoss ? 2.8 : 2.0;

      const target = player.pos.clone();
      target.y = 1.3;

      const dir = target.sub(start).normalize();
      dir.x += rand(-0.04, 0.04);
      dir.y += rand(-0.02, 0.04);
      dir.z += rand(-0.04, 0.04);
      dir.normalize();

      const book = ohMakeBookProjectile(start, dir);
      state.enemyBullets.push(book);

      createFlash?.(start.clone(), 0xffd166, 1.0, 3.2, 0.05);
      return;
    }

    return _enemyShoot(enemy);
  };

  const _updateEnemies = updateEnemies;
  updateEnemies = function(dt){
    if(ohFreezeActive()) return;

    _updateEnemies(dt);

    for(const enemy of state.enemies){
      if(!enemy?.mesh) continue;

      const name = ohFindNamedEnemy(enemy);

      if(name === "Patrick"){
        enemy.alwaysFlee = false;
        enemy.neverShoot = false;
        enemy.usesGrenades = true;
        enemy.preferredDistance = 2.4;
      }

      if(name === "Elly"){
        enemy.preferredDistance = 3.2;
        enemy.speed = Math.max(enemy.speed || 0, 5.6);
      }

      if(name === "Mark"){
        enemy.preferredDistance = 2.1;
      }
    }
  };

  const _updateMovement = updateMovement;
  updateMovement = function(dt){
    if(ohFreezeActive()) return;
    return _updateMovement(dt);
  };

  const _updateBullets = updateBullets;
  updateBullets = function(dt){
    if(ohFreezeActive()) return;
    return _updateBullets(dt);
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    const name = ohFindNamedEnemy(enemy);

    if(name === "Pieter"){
      ohPieterSting();
      ohFreezeGame(3);
    }

    return _killEnemy(enemy);
  };

  // direct bestaande enemies bijwerken als je tijdens een run plakt
  for(const enemy of state.enemies){
    ohStyleKevin(enemy);
  }

  showFloating?.("OH extra named patch online");
})();



function ohLightGrenadeExplosion(position, radius, damage, color = 0x9dff7c){
  createBurst(position, color, 8, 4.2, { minSize:.04, maxSize:.09, minLife:.18, maxLife:.42, maxUp:1.05, drag:0.91, gravity:3.1, shrink:0.975 });
  createBurst(position, 0xffffff, 3, 2.8, { minSize:.025, maxSize:.05, minLife:.08, maxLife:.18, gravity:1.2, shrink:0.94 });
  createBurst(position, 0x23301c, 5, 2.0, { minSize:.06, maxSize:.12, minLife:.22, maxLife:.52, minUp:.03, maxUp:.4, drag:0.95, gravity:0.55, shrink:0.986 });
  createShockwave(position, color, Math.max(1.3, radius * 0.82));
  createFlash(position, color, 1.45, radius * 2.8, 0.11);

  for(let i = state.enemies.length - 1; i >= 0; i--){
    const e = state.enemies[i];
    if(!e?.mesh) continue;
    const hitPos = e.mesh.position.clone();
    hitPos.y = 1.7;
    const d = hitPos.distanceTo(position);
    if(d >= radius) continue;
    e.hp -= damage * (1 - d / radius);
    if(e.hp <= 0){
      killEnemy(e);
      state.enemies.splice(i, 1);
    }
  }

  if(state.boss?.mesh){
    const bp = state.boss.mesh.position.clone();
    bp.y = 2.2;
    const d = bp.distanceTo(position);
    if(d < radius){
      state.boss.hp -= damage * (1 - d / radius);
      updateBossBar?.();
      if(state.boss.hp <= 0) killEnemy(state.boss);
    }
  }
}

const _explodeAt_ohLight = explodeAt;
explodeAt = function(position, radius, damage, color){
  const grenadeColors = new Set([0x9dff7c, 0xc9ff9d, 0xd8ffca]);
  if(grenadeColors.has(color)){
    return ohLightGrenadeExplosion(position, radius, damage, color);
  }
  return _explodeAt_ohLight(position, radius, damage, color);
};

state.ohEnemyGrenades = state.ohEnemyGrenades || [];
const _updateBullets_ohGrenades = updateBullets;
updateBullets = function(dt){
  _updateBullets_ohGrenades(dt);

  for(let i = state.ohEnemyGrenades.length - 1; i >= 0; i--){
    const g = state.ohEnemyGrenades[i];
    if(!g?.mesh){
      state.ohEnemyGrenades.splice(i, 1);
      continue;
    }

    g.mesh.position.addScaledVector(g.vel, dt);
    g.vel.y -= (g.gravity || 9.6) * dt;
    g.life -= dt;
    g.mesh.rotation.x += (g.spin || 0) * dt;
    g.mesh.rotation.z += (g.spin || 0) * dt * 0.7;

    g.trailClock = (g.trailClock || 0) + dt;
    if(g.trailClock >= (g.trailEvery || 0.05)){
      g.trailClock = 0;
      createBurst(g.mesh.position.clone(), g.trailColor || g.explosionColor || 0x9dff7c, 1, 0.9, {
        minSize:.025, maxSize:.045, minLife:.06, maxLife:.12, minUp:.02, maxUp:.18, drag:0.88, gravity:0.4, shrink:0.93
      });
    }

    let explode = g.life <= 0 || g.mesh.position.y <= 0.2 || collidesAt(g.mesh.position.x, g.mesh.position.z, 0.14);

    const playerHit = new THREE.Vector3(player.pos.x, 1.0, player.pos.z);
    const distToPlayer = g.mesh.position.distanceTo(playerHit);
    if(!explode && distToPlayer < 1.1){
      explode = true;
    }

    if(explode){
      const pos = g.mesh.position.clone();
      const radius = g.radius || 2.7;
      const damage = g.damage || 18;
      const playerDist = player.pos.distanceTo(pos);
      if(playerDist < radius + 0.25){
        const scale = Math.max(0.35, 1 - (playerDist / Math.max(radius, 0.001)));
        applyDamage(Math.round(damage * scale));
      }
      ohLightGrenadeExplosion(pos, radius, damage, g.explosionColor || 0x9dff7c);
      scene.remove(g.mesh);
      state.ohEnemyGrenades.splice(i, 1);
    }
  }
};

/* ==========================================
   OH WEAPON SCROLL + 6 SLOT LOADOUT PATCH
   Plak dit HELE blok onderaan je code
   ========================================== */
(() => {
  const LOADOUT_ORDER = ["bullet", "rocket", "grenade", "plasma", "mine", "orbital"];

  function slotLabel(slot){
    return slot === "bullet"  ? "Bullet Rifle"  :
           slot === "rocket"  ? "Rocket Launcher" :
           slot === "grenade" ? "Grenade Lobber" :
           slot === "plasma"  ? "Plasma Burst" :
           slot === "mine"    ? "Shock Mine" :
           slot === "orbital" ? "Orbital Beam" :
           "Weapon";
  }

  function slotShort(slot){
    return slot === "bullet"  ? "Bullet"  :
           slot === "rocket"  ? "Rocket"  :
           slot === "grenade" ? "Grenade" :
           slot === "plasma"  ? "Plasma"  :
           slot === "mine"    ? "Mine"    :
           slot === "orbital" ? "Orbital" :
           "Weapon";
  }

  function slotResourceText(slot){
    return slot === "bullet"  ? `${player.ammo.bullet} ammo` :
           slot === "rocket"  ? `${player.ammo.rocket} ammo` :
           slot === "grenade" ? `${player.ammo.grenade} ammo` :
           slot === "plasma"  ? `${player.abilities.plasma} charge` + (player.abilities.plasma === 1 ? "" : "s") :
           slot === "mine"    ? `${player.abilities.mine} charge` + (player.abilities.mine === 1 ? "" : "s") :
           slot === "orbital" ? `${player.abilities.orbital} charge` + (player.abilities.orbital === 1 ? "" : "s") :
           "";
  }

  function slotAvailable(slot){
    return slot === "bullet"  ? player.ammo.bullet > 0 :
           slot === "rocket"  ? player.ammo.rocket > 0 :
           slot === "grenade" ? player.ammo.grenade > 0 :
           slot === "plasma"  ? player.abilities.plasma > 0 :
           slot === "mine"    ? player.abilities.mine > 0 :
           slot === "orbital" ? player.abilities.orbital > 0 :
           false;
  }

  function chipForSlot(slot){
    return slot === "bullet"  ? ui.chipBullet :
           slot === "rocket"  ? ui.chipRocket :
           slot === "grenade" ? ui.chipGrenade :
           slot === "plasma"  ? ui.chipPlasma :
           slot === "mine"    ? ui.chipMine :
           slot === "orbital" ? ui.chipOrbital :
           null;
  }

  function slotIndex(slot){
    const i = LOADOUT_ORDER.indexOf(slot);
    return i >= 0 ? i : 0;
  }

  function updateWeaponChipTexts(){
    if(ui.chipBullet)  ui.chipBullet.textContent  = `1 ${slotShort("bullet")} · ${player.ammo.bullet}`;
    if(ui.chipRocket)  ui.chipRocket.textContent  = `2 ${slotShort("rocket")} · ${player.ammo.rocket}`;
    if(ui.chipGrenade) ui.chipGrenade.textContent = `3 ${slotShort("grenade")} · ${player.ammo.grenade}`;
    if(ui.chipPlasma)  ui.chipPlasma.textContent  = `4 ${slotShort("plasma")} · ${player.abilities.plasma}`;
    if(ui.chipMine)    ui.chipMine.textContent    = `5 ${slotShort("mine")} · ${player.abilities.mine}`;
    if(ui.chipOrbital) ui.chipOrbital.textContent = `6 ${slotShort("orbital")} · ${player.abilities.orbital}`;

    LOADOUT_ORDER.forEach(slot => {
      const chip = chipForSlot(slot);
      if(!chip) return;
      chip.classList.toggle("active", player.weapon === slot);
      chip.classList.toggle("empty", !slotAvailable(slot));
      chip.style.opacity = slotAvailable(slot) ? "1" : "0.45";
    });
  }

  function updateSelectedWeaponUI(){
    if(ui.weaponName){
      ui.weaponName.textContent = `${slotLabel(player.weapon)} · ${slotResourceText(player.weapon)}`;
    }
    updateWeaponChipTexts();
  }

  function cycleWeapon(dir = 1){
    let idx = slotIndex(player.weapon);
    idx = (idx + dir + LOADOUT_ORDER.length) % LOADOUT_ORDER.length;
    setWeapon(LOADOUT_ORDER[idx]);
  }

  weaponLabel = function(w){
    return slotLabel(w);
  };

  setWeapon = function(w){
    if(!LOADOUT_ORDER.includes(w)) w = "bullet";
    player.weapon = w;
    updateSelectedWeaponUI();

    if(w === "plasma"){
      flashHint?.(`Plasma Burst geselecteerd · ${player.abilities.plasma} charge(s)`);
    }else if(w === "mine"){
      flashHint?.(`Shock Mine geselecteerd · ${player.abilities.mine} charge(s)`);
    }else if(w === "orbital"){
      flashHint?.(`Orbital Beam geselecteerd · ${player.abilities.orbital} charge(s)`);
    }
  };

  ensureUsableWeapon = function(){
    if(slotAvailable(player.weapon)) return;

    const currentIndex = slotIndex(player.weapon);

    for(let step = 1; step <= LOADOUT_ORDER.length; step++){
      const next = LOADOUT_ORDER[(currentIndex + step) % LOADOUT_ORDER.length];
      if(slotAvailable(next)){
        setWeapon(next);
        return;
      }
    }

    setWeapon("bullet");
  };

  const _setStat = setStat;
  setStat = function(){
    _setStat();
    updateSelectedWeaponUI();
  };

  const _shootWithDirection = shootWithDirection;
  shootWithDirection = function(dirOverride = null){
    if(!state.running || !player.alive) return false;

    if(player.weapon === "plasma"){
      if(player.abilities.plasma <= 0){
        flashHint?.("Geen plasma charges");
        ensureUsableWeapon();
        return false;
      }
      firePlasmaBurst();
      updateSelectedWeaponUI();
      return true;
    }

    if(player.weapon === "mine"){
      if(player.abilities.mine <= 0){
        flashHint?.("Geen mines over");
        ensureUsableWeapon();
        return false;
      }
      deployShockMine();
      updateSelectedWeaponUI();
      return true;
    }

    if(player.weapon === "orbital"){
      if(player.abilities.orbital <= 0){
        flashHint?.("Geen orbital charges");
        ensureUsableWeapon();
        return false;
      }
      deployOrbital();
      updateSelectedWeaponUI();
      return true;
    }

    const ok = _shootWithDirection(dirOverride);
    updateSelectedWeaponUI();
    return ok;
  };

  function handleDirectSlotSelect(slot){
    setWeapon(slot);
  }

  window.addEventListener("wheel", e => {
    if(!state.running) return;
    if(Math.abs(e.deltaY) < 4) return;

    e.preventDefault();
    cycleWeapon(e.deltaY > 0 ? 1 : -1);
  }, { passive:false });

  window.addEventListener("keydown", e => {
    if(!state.running && !["Digit1","Digit2","Digit3","Digit4","Digit5","Digit6"].includes(e.code)) return;

    if(e.code === "Digit1"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("bullet");
    } else if(e.code === "Digit2"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("rocket");
    } else if(e.code === "Digit3"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("grenade");
    } else if(e.code === "Digit4"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("plasma");
    } else if(e.code === "Digit5"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("mine");
    } else if(e.code === "Digit6"){
      e.preventDefault();
      e.stopImmediatePropagation();
      handleDirectSlotSelect("orbital");
    }
  }, true);

  [ui.chipBullet, ui.chipRocket, ui.chipGrenade, ui.chipPlasma, ui.chipMine, ui.chipOrbital].forEach((chip, i) => {
    if(!chip || chip.dataset.ohScrollBound) return;
    chip.dataset.ohScrollBound = "1";
    chip.style.pointerEvents = "auto";
    chip.style.cursor = "pointer";
    chip.addEventListener("click", () => {
      setWeapon(LOADOUT_ORDER[i]);
    });
  });

  if(!state.weaponScrollHintShown){
    state.weaponScrollHintShown = true;
    flashHint?.("Scroll of druk 1-6 om tussen alle 6 loadout-slots te wisselen");
  }

  if(!LOADOUT_ORDER.includes(player.weapon)){
    player.weapon = "bullet";
  }

  updateSelectedWeaponUI();
})();

/* ==========================================
   OH JOOST SMOKE PATCH
   Plak dit HELE blok onderaan je code
   ========================================== */
(() => {
  function ohEnemyName(enemy){
    return enemy?.enemyName || enemy?.mesh?.userData?.enemyName || "";
  }

  function spawnJoostSmoke(enemy, dt){
    if(!enemy?.mesh) return;

    enemy._joostSmokeTimer = (enemy._joostSmokeTimer || 0) - dt;
    if(enemy._joostSmokeTimer > 0) return;

    enemy._joostSmokeTimer = 0.045;

    const origin = enemy.mesh.position.clone();
    origin.y += 1.15 + Math.random() * 0.55;
    origin.x += rand(-0.22, 0.22);
    origin.z += rand(-0.22, 0.22);

    const puff = new THREE.Mesh(
      new THREE.SphereGeometry(rand(0.10, 0.18), 8, 8),
      new THREE.MeshBasicMaterial({
        color: 0xdfe6ee,
        transparent: true,
        opacity: rand(0.16, 0.28)
      })
    );

    puff.position.copy(origin);
    scene.add(puff);

    state.particles.push({
      mesh: puff,
      vel: new THREE.Vector3(
        rand(-0.25, 0.25),
        rand(0.22, 0.55),
        rand(-0.25, 0.25)
      ),
      life: rand(0.55, 1.05),
      drag: 0.95,
      gravity: -0.06,
      shrink: 1.01,
      rotate: rand(-1.2, 1.2),
      smoke: true
    });
  }

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss = false){
    const before = state.enemies.length;
    const hadBoss = !!state.boss;

    const result = _spawnEnemy(isBoss);

    let enemy = null;
    if(isBoss){
      enemy = state.boss;
    } else if(state.enemies.length > before){
      enemy = state.enemies[state.enemies.length - 1];
    } else if(!hadBoss && isBoss && state.boss){
      enemy = state.boss;
    }

    if(enemy && ohEnemyName(enemy) === "Joost"){
      enemy.hasJoostSmoke = true;
      enemy._joostSmokeTimer = 0.02;
    }

    return result;
  };

  const _updateEnemies = updateEnemies;
  updateEnemies = function(dt){
    _updateEnemies(dt);

    for(const enemy of state.enemies){
      if(ohEnemyName(enemy) === "Joost"){
        enemy.hasJoostSmoke = true;
        spawnJoostSmoke(enemy, dt);
      }
    }

    if(state.boss && ohEnemyName(state.boss) === "Joost"){
      state.boss.hasJoostSmoke = true;
      spawnJoostSmoke(state.boss, dt);
    }
  };

  // ook direct toepassen op enemies die al bestaan
  for(const enemy of state.enemies){
    if(ohEnemyName(enemy) === "Joost"){
      enemy.hasJoostSmoke = true;
      enemy._joostSmokeTimer = 0.02;
    }
  }

  if(state.boss && ohEnemyName(state.boss) === "Joost"){
    state.boss.hasJoostSmoke = true;
    state.boss._joostSmokeTimer = 0.02;
  }

  showFloating?.("Joost smoke online");
})();

/* ==========================================
   OH NAME TWEAKS PATCH
   Lisa = altijd +300
   Joost death = rookbom
   Jos = groter monster
   Plak dit HELE blok onderaan je code
   ========================================== */
(() => {
  function ohNameOf(enemy){
    return enemy?.enemyName || enemy?.mesh?.userData?.enemyName || "";
  }

  function ohSpawnSmokeBomb(pos){
    if(!pos) return;

    for(let i = 0; i < 26; i++){
      const puff = new THREE.Mesh(
        new THREE.SphereGeometry(rand(0.14, 0.28), 8, 8),
        new THREE.MeshBasicMaterial({
          color: 0xd9e2ea,
          transparent: true,
          opacity: rand(0.16, 0.28)
        })
      );

      puff.position.copy(pos);
      puff.position.x += rand(-0.35, 0.35);
      puff.position.y += rand(0.2, 1.0);
      puff.position.z += rand(-0.35, 0.35);

      scene.add(puff);

      state.particles.push({
        mesh: puff,
        vel: new THREE.Vector3(
          rand(-1.8, 1.8),
          rand(1.4, 3.2),
          rand(-1.8, 1.8)
        ),
        life: rand(0.7, 1.4),
        drag: 0.93,
        gravity: -0.18,
        rotate: rand(-4, 4),
        shrink: 1.01,
        smoke: true
      });
    }

    createShockwave?.(pos.clone(), 0xcfd8df, 1.8);
    createFlash?.(pos.clone().add(new THREE.Vector3(0, 0.8, 0)), 0xdfe7ee, 0.9, 2.2, 0.04);
  }

  function ohApplyJosScale(enemy){
    if(!enemy?.mesh || enemy.isBoss) return;
    if(ohNameOf(enemy) !== "Jos") return;
    if(enemy.__ohJosScaled) return;

    enemy.mesh.scale.multiplyScalar(1.18);
    enemy.radius = (enemy.radius || 1) * 1.12;
    enemy.maxHp = Math.round(enemy.maxHp * 1.08);
    enemy.hp = Math.min(enemy.maxHp, Math.round(enemy.hp * 1.08));

    if(enemy.groundRing){
      enemy.groundRing.scale.multiplyScalar(1.14);
    }

    enemy.__ohJosScaled = true;
  }

  function ohLisaMoneyBurst(enemy){
    if(!enemy?.mesh) return;

    const origin = enemy.mesh.position.clone();
    origin.y += 1.35;

    for(let i = 0; i < 12; i++){
      const bill = new THREE.Mesh(
        new THREE.PlaneGeometry(0.22, 0.12),
        new THREE.MeshBasicMaterial({
          color: 0x4cff7a,
          transparent: true,
          opacity: 0.92,
          side: THREE.DoubleSide
        })
      );

      bill.position.copy(origin);
      bill.rotation.set(rand(-0.5, 0.5), rand(0, Math.PI), rand(-0.5, 0.5));
      scene.add(bill);

      state.particles.push({
        mesh: bill,
        vel: new THREE.Vector3(
          rand(-2.3, 2.3),
          rand(2.0, 4.0),
          rand(-2.3, 2.3)
        ),
        life: rand(0.55, 1.05),
        drag: 0.92,
        gravity: -3.4,
        rotate: rand(-7, 7),
        shrink: 0.985
      });
    }

    createFlash?.(
      enemy.mesh.position.clone().add(new THREE.Vector3(0, 1.35, 0)),
      0x4cff7a,
      1.2,
      3.2,
      0.05
    );
  }

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss = false){
    const beforeCount = state.enemies.length;
    const hadBoss = !!state.boss;

    const result = _spawnEnemy(isBoss);

    let enemy = null;
    if(isBoss){
      enemy = state.boss;
    } else if(state.enemies.length > beforeCount){
      enemy = state.enemies[state.enemies.length - 1];
    } else if(!hadBoss && isBoss && state.boss){
      enemy = state.boss;
    }

    if(enemy){
      ohApplyJosScale(enemy);
    }

    return result;
  };

  const _updateBullets = updateBullets;
  updateBullets = function(dt){
    const hpBefore = new Map();
    for(const enemy of state.enemies){
      hpBefore.set(enemy, enemy.hp);
    }

    const bossRef = state.boss;
    const bossHpBefore = bossRef ? bossRef.hp : null;

    _updateBullets(dt);

    for(const [enemy, before] of hpBefore.entries()){
      if(enemy && typeof before === "number" && enemy.hp < before){
        if(ohNameOf(enemy) === "Lisa"){
          player.score += 300;
          showFloating("LISA +300");
          ohLisaMoneyBurst(enemy);
          setStat?.();
        }
      }
    }

    if(bossRef && typeof bossHpBefore === "number" && bossRef.hp < bossHpBefore){
      if(ohNameOf(bossRef) === "Lisa"){
        player.score += 300;
        showFloating("LISA +300");
        ohLisaMoneyBurst(bossRef);
        setStat?.();
      }
    }
  };

  const _damageEnemyDirect = typeof damageEnemyDirect === "function" ? damageEnemyDirect : null;
  if(_damageEnemyDirect){
    damageEnemyDirect = function(enemy, damage){
      const before = enemy?.hp;
      const result = _damageEnemyDirect(enemy, damage);

      if(result && enemy && typeof before === "number" && enemy.hp < before){
        if(ohNameOf(enemy) === "Lisa"){
          player.score += 300;
          showFloating("LISA +300");
          ohLisaMoneyBurst(enemy);
          setStat?.();
        }
      }

      return result;
    };
  }

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    if(ohNameOf(enemy) === "Joost" && enemy?.mesh?.position){
      ohSpawnSmokeBomb(enemy.mesh.position.clone());
      showFloating("JOOST SMOKEBOMB");
    }

    return _killEnemy(enemy);
  };

  for(const enemy of state.enemies){
    ohApplyJosScale(enemy);
  }
  if(state.boss){
    ohApplyJosScale(state.boss);
  }

  showFloating?.("Lisa / Joost / Jos patch online");
})();


/* =========================
   OLDE HANTER PROFESSIONAL ARENA / ARMORY / BOSS EXPANSION
   plak dit direct boven: animate(performance.now());
   ========================= */
(() => {
  const bossBarLabelEl = document.getElementById("bossBarLabel");

  function injectDirectorStyles(){
    if(document.getElementById("ohDirectorStyles")) return;
    const style = document.createElement("style");
    style.id = "ohDirectorStyles";
    style.textContent = `
      #directorHud{
        position:fixed;
        top:14px;
        right:14px;
        z-index:22;
        width:min(320px, calc(100vw - 28px));
        background:linear-gradient(180deg, rgba(8,16,34,.88), rgba(8,11,24,.74));
        border:1px solid rgba(126,213,255,.18);
        box-shadow:0 18px 40px rgba(0,0,0,.28);
        border-radius:18px;
        padding:12px 14px;
        backdrop-filter:blur(10px);
        pointer-events:none;
      }
      #directorHud .dir-top{
        display:flex;
        justify-content:space-between;
        gap:8px;
        align-items:flex-start;
        margin-bottom:10px;
      }
      #directorHud .dir-title{
        font-size:13px;
        font-weight:800;
        letter-spacing:.08em;
        text-transform:uppercase;
      }
      #directorHud .dir-sub{
        color:rgba(227,242,255,.72);
        font-size:11px;
        margin-top:2px;
      }
      #directorHud .dir-chip{
        border-radius:999px;
        padding:5px 9px;
        border:1px solid rgba(255,255,255,.12);
        background:rgba(255,255,255,.06);
        font-size:11px;
        color:rgba(255,255,255,.88);
      }
      #directorHud .dir-grid{
        display:grid;
        grid-template-columns:1fr 1fr;
        gap:8px;
      }
      #directorHud .dir-card{
        border-radius:12px;
        border:1px solid rgba(255,255,255,.09);
        background:rgba(255,255,255,.04);
        padding:8px 10px;
      }
      #directorHud .dir-card b{ display:block; font-size:11px; margin-bottom:4px; color:rgba(255,255,255,.74); }
      #directorHud .dir-card span{ display:block; font-size:12px; line-height:1.35; }
      #directorHud .dir-card em{ font-style:normal; color:#8cecff; }
      #directorHud .dir-foot{
        margin-top:9px;
        font-size:11px;
        color:rgba(255,255,255,.72);
        min-height:16px;
      }
      @media (max-width: 960px){
        #directorHud{ top:auto; right:10px; bottom:10px; width:min(320px, calc(100vw - 20px)); }
      }
    `;
    document.head.appendChild(style);
  }

  function buildDirectorHud(){
    if(document.getElementById("directorHud")) return;
    injectDirectorStyles();
    const hud = document.createElement("div");
    hud.id = "directorHud";
    hud.innerHTML = `
      <div class="dir-top">
        <div>
          <div class="dir-title">Arena Director</div>
          <div class="dir-sub" id="dirSector">Sector: Core Nexus</div>
        </div>
        <div class="dir-chip" id="dirThreat">Threat LOW</div>
      </div>
      <div class="dir-grid">
        <div class="dir-card"><b>Weapon Mod</b><span id="dirWeapon">Arc rounds calibreren…</span></div>
        <div class="dir-card"><b>Boss Intel</b><span id="dirBoss">Nog geen boss op radar</span></div>
        <div class="dir-card"><b>Map Layer</b><span id="dirMap">Cover pods, lanes en chokepoints actief</span></div>
        <div class="dir-card"><b>Wave Directive</b><span id="dirDirective">Hou mobiel momentum en pak pickups snel op</span></div>
      </div>
      <div class="dir-foot" id="dirFoot">Nieuwe arena modules geladen.</div>
    `;
    document.body.appendChild(hud);
  }
  buildDirectorHud();

  const directorHud = {
    sector: document.getElementById("dirSector"),
    threat: document.getElementById("dirThreat"),
    weapon: document.getElementById("dirWeapon"),
    boss: document.getElementById("dirBoss"),
    map: document.getElementById("dirMap"),
    directive: document.getElementById("dirDirective"),
    foot: document.getElementById("dirFoot")
  };

  const director = {
    shotCounter: { bullet:0, rocket:0, grenade:0 },
    panelTimer: 0,
    sectorName: "Core Nexus",
    lastBossName: "Nog geen boss op radar",
    waveDirective: "Hou mobiel momentum en pak pickups snel op",
    mapLabel: "Cover pods, lanes en chokepoints actief",
    projectileCache: new WeakMap(),
    bossProfiles: [
      {
        id: "warden",
        label: "Aegis Warden",
        hpMul: 1.28,
        speedMul: 0.94,
        tint: 0x7ec8ff,
        ringColor: 0x7ec8ff,
        threat: "Area denial / guards",
        intro: "AEGIS WARDEN ARRIVES",
        directive: "Gebruik cover, vermijd de pulse ring en schakel guards snel uit."
      },
      {
        id: "phantom",
        label: "Shade Phantom",
        hpMul: 0.96,
        speedMul: 1.18,
        tint: 0xff7fcf,
        ringColor: 0xff7fcf,
        threat: "Dashes / mirror fire",
        intro: "SHADE PHANTOM BREACH",
        directive: "Blijf bewegen; de phantom straft stilstand met dash slashes."
      },
      {
        id: "siegebreaker",
        label: "Siegebreaker Prime",
        hpMul: 1.12,
        speedMul: 1.00,
        tint: 0xffc86f,
        ringColor: 0xffc86f,
        threat: "Mortars / missile zones",
        intro: "SIEGEBREAKER PRIME LOCKED",
        directive: "Lees telegraphs vroeg en forceer rotatie tussen de lanes."
      }
    ]
  };

  function tintMesh(group, tint){
    group?.traverse?.(obj => {
      if(!obj.material) return;
      const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
      mats.forEach(mat => {
        if(mat.emissive && typeof mat.emissive.getHex === "function"){
          const mix = new THREE.Color(tint);
          mat.emissive.lerp(mix, 0.16);
          mat.emissiveIntensity = Math.max(mat.emissiveIntensity || 0, 0.38);
        }
        if(mat.color && typeof mat.color.getHex === "function"){
          const c = new THREE.Color(tint);
          mat.color.lerp(c, 0.05);
        }
      });
    });
  }

  function addCoverPod(x, z, colorA=0x244d96, colorB=0x5c2f88){
    addBox(6.5, 2.6, 1.6, x, 1.3, z - 3.0, colorA);
    addBox(6.5, 2.6, 1.6, x, 1.3, z + 3.0, colorA);
    addBox(1.6, 2.8, 4.4, x - 3.0, 1.4, z, colorB);
    addBox(1.6, 2.8, 4.4, x + 3.0, 1.4, z, colorB);
    addCylinderCollider(0.85, 3.1, x, 1.55, z, 0x18284e);
    addDecalRing(x, z, 3.8, colorA);
  }

  function addLaneGate(x, z, wide=true, color=0x2bc1ff){
    const width = wide ? 12 : 8;
    addBox(width, 0.8, 2.0, x, 0.4, z, 0x2a3c63);
    addBox(1.8, 4.6, 2.2, x - width * 0.5 + 0.8, 2.3, z, color);
    addBox(1.8, 4.6, 2.2, x + width * 0.5 - 0.8, 2.3, z, color);
    const holo = new THREE.Mesh(
      new THREE.PlaneGeometry(width * 0.8, 2.4),
      new THREE.MeshBasicMaterial({ color, transparent:true, opacity:0.14, side:THREE.DoubleSide, depthWrite:false })
    );
    holo.position.set(x, 2.0, z);
    scene.add(holo);
    director.projectileCache.set(holo, { phase: Math.random() * Math.PI * 2, baseY: holo.position.y });
  }

  function addArenaExpansion(){
    [
      [-40, -32, 0x2b86ff, 0x8038ff],
      [40, -32, 0xff5aaa, 0x2b86ff],
      [-40, 32, 0xffc76b, 0x2b86ff],
      [40, 32, 0x5ff1c2, 0xff5aaa],
      [-24, 0, 0x2bc1ff, 0x5f3cff],
      [24, 0, 0xff6ea1, 0x2bc1ff]
    ].forEach(([x, z, a, b]) => addCoverPod(x, z, a, b));

    addLaneGate(0, -18, true, 0x4df7ff);
    addLaneGate(0, 40, true, 0xff8dd8);
    addLaneGate(-47, 0, false, 0x7ec8ff);
    addLaneGate(47, 0, false, 0xffd166);

    for(let i=0; i<8; i++){
      const beacon = new THREE.Mesh(
        new THREE.CylinderGeometry(0.22, 0.26, 4.6, 10),
        new THREE.MeshStandardMaterial({ color:0x1b2338, emissive:i % 2 ? 0x2bc1ff : 0xff6ea1, emissiveIntensity:0.7, metalness:0.45, roughness:0.34 })
      );
      const angle = (i / 8) * Math.PI * 2;
      beacon.position.set(Math.cos(angle) * 26, 2.3, Math.sin(angle) * 26);
      scene.add(beacon);
      addDecalRing(beacon.position.x, beacon.position.z, 1.45, i % 2 ? 0x2bc1ff : 0xff6ea1);
    }

    const skylineBanner = new THREE.Group();
    for(let i=0;i<5;i++){
      const banner = new THREE.Mesh(
        new THREE.PlaneGeometry(10, 3),
        new THREE.MeshBasicMaterial({ color:i % 2 ? 0x4df7ff : 0xff4fd8, transparent:true, opacity:0.11, side:THREE.DoubleSide, depthWrite:false })
      );
      banner.position.set(-36 + i * 18, 9 + i * 0.3, i % 2 ? 53 : -53);
      banner.rotation.y = i % 2 ? 0 : Math.PI;
      skylineBanner.add(banner);
      director.projectileCache.set(banner, { phase: i * 0.8, baseY: banner.position.y });
    }
    scene.add(skylineBanner);

    const oldAnimate = animate;
    animate = function(now){
      skylineBanner.children.forEach((banner, i) => {
        const d = director.projectileCache.get(banner);
        banner.position.y = d.baseY + Math.sin(now * 0.001 + d.phase + i) * 0.25;
        banner.material.opacity = 0.08 + Math.sin(now * 0.0018 + d.phase) * 0.03;
      });
      oldAnimate(now);
    };
  }
  addArenaExpansion();

  function sectorName(){
    const x = player.pos.x;
    const z = player.pos.z;
    if(Math.abs(x) < 14 && Math.abs(z) < 14) return "Core Nexus";
    if(z < -18 && Math.abs(x) < 22) return "North Breach";
    if(z > 18 && Math.abs(x) < 22) return "South Ruins";
    if(x < -18 && Math.abs(z) < 24) return "West Bastion";
    if(x > 18 && Math.abs(z) < 24) return "East Relay";
    if(x < -24 && z < -16) return "Cryo Yard";
    if(x > 24 && z < -16) return "Neon Foundry";
    if(x < -24 && z > 16) return "Obsidian Ward";
    if(x > 24 && z > 16) return "Signal Docks";
    return "Outer Ring";
  }

  function currentWeaponDirective(){
    const w = player.weapon;
    if(w === "bullet") return `Arc round elke 6e kogel · teller ${director.shotCounter.bullet % 6 || 6}/6`;
    if(w === "rocket") return "Rockets laten napalm pools achter op impact";
    return "Grenades splitsen in cluster shrapnel bij detonatie";
  }

  function refreshDirectorHud(force=false){
    director.panelTimer -= force ? 999 : 0.12;
    if(director.panelTimer > 0 && !force) return;
    director.panelTimer = 0.18;
    director.sectorName = sectorName();
    const threat = state.boss ? "MAX" : player.wave >= 10 ? "HIGH" : player.wave >= 5 ? "MED" : "LOW";
    if(directorHud.sector) directorHud.sector.textContent = `Sector: ${director.sectorName}`;
    if(directorHud.threat) directorHud.threat.textContent = `Threat ${threat}`;
    if(directorHud.weapon) directorHud.weapon.textContent = currentWeaponDirective();
    if(directorHud.boss) directorHud.boss.textContent = state.boss ? `${director.lastBossName} · fase ${state.boss.phase || 1}` : director.lastBossName;
    if(directorHud.map) directorHud.map.textContent = director.mapLabel;
    if(directorHud.directive) directorHud.directive.textContent = director.waveDirective;
    if(directorHud.foot) directorHud.foot.textContent = state.boss
      ? `Boss actief in ${director.sectorName}. Hou ruimte tussen jou en de arena telegraphs.`
      : `Wave ${player.wave} · ${state.enemies.length} hostiles · combo x${(state.combo || 1).toFixed(1)}`;
  }

  const _createProjectile = createProjectile;
  createProjectile = function(pos, dir, config){
    const proj = _createProjectile(pos, dir, config);
    if(config?.friendly){
      if(config.type === "bullet" && player.weapon === "bullet"){
        director.shotCounter.bullet += 1;
        if(director.shotCounter.bullet % 6 === 0){
          proj.arcRound = true;
          proj.damage += 10;
          proj.life += 0.25;
          proj.mesh.scale.multiplyScalar(1.22);
          if(proj.mesh.material?.color) proj.mesh.material.color.setHex(0x7ef5ff);
          proj.trailColor = 0x7ef5ff;
        }
      } else if(config.type === "rocket" && player.weapon === "rocket"){
        director.shotCounter.rocket += 1;
        proj.napalm = true;
        proj.radius = (proj.radius || 0) + 0.8;
      } else if(config.type === "grenade" && player.weapon === "grenade"){
        director.shotCounter.grenade += 1;
        proj.cluster = true;
        proj.radius = (proj.radius || 0) + 0.4;
      }
    }
    return proj;
  };

  function spawnNapalmPool(position){
    const ring = new THREE.Mesh(
      new THREE.RingGeometry(1.6, 2.8, 34),
      new THREE.MeshBasicMaterial({ color:0xff9f55, transparent:true, opacity:0.42, side:THREE.DoubleSide })
    );
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(position.x, 0.05, position.z);
    const core = new THREE.Mesh(
      new THREE.CircleGeometry(1.5, 28),
      new THREE.MeshBasicMaterial({ color:0xff6622, transparent:true, opacity:0.16, side:THREE.DoubleSide })
    );
    core.rotation.x = -Math.PI / 2;
    core.position.set(position.x, 0.035, position.z);
    scene.add(ring, core);
    state.hazards.push({ kind:"napalm", mesh:ring, core, life:5.0, pulse:0, radius:3.4, damage:10 });
    createBurst(position.clone().add(new THREE.Vector3(0,0.5,0)), 0xffa866, 16, 3.6, { minLife:.16, maxLife:.42, gravity:1.8, shrink:0.95 });
    createFlash(position.clone().add(new THREE.Vector3(0,1.0,0)), 0xff8a42, 2.4, 7.5, 0.12);
  }

  function spawnClusterShards(position){
    createBurst(position.clone().add(new THREE.Vector3(0,0.5,0)), 0xc7ff9d, 10, 3.8, { minLife:.16, maxLife:.38, gravity:1.3, shrink:0.95 });
    for(let i=0;i<6;i++){
      const angle = (i / 6) * Math.PI * 2;
      const dir = new THREE.Vector3(Math.cos(angle), 0.10 + Math.random() * 0.08, Math.sin(angle)).normalize();
      state.bullets.push(_createProjectile(position.clone().add(new THREE.Vector3(0,0.5,0)), dir, {
        speed: 19,
        friendly: true,
        color: 0xcaff9d,
        trailColor: 0xf0ffd8,
        size: 0.08,
        life: 0.85,
        damage: 7,
        type: "bullet"
      }));
    }
  }

  function dischargeArc(position){
    const candidates = [];
    if(state.boss?.mesh) candidates.push(state.boss);
    for(const e of state.enemies) if(e?.mesh) candidates.push(e);
    candidates
      .filter(e => e.mesh.position.distanceTo(position) < 7.5)
      .sort((a, b) => a.mesh.position.distanceTo(position) - b.mesh.position.distanceTo(position))
      .slice(0, 2)
      .forEach((enemy, idx) => {
        const target = enemy.mesh.position.clone().add(new THREE.Vector3(0, enemy.isBoss ? 2.2 : 1.3, 0));
        createFlash(target.clone(), 0x8bf0ff, 1.5, 5.5, 0.08);
        createBurst(target.clone(), 0x9ffcff, 6, 2.6, { minLife:.10, maxLife:.24, gravity:0.4, shrink:0.93 });
        damageEnemyDirect(enemy, 8 + idx * 2);
      });
  }

  const _updateBullets = updateBullets;
  updateBullets = function(dt){
    const tracked = new Map();
    for(const b of state.bullets){
      if(b?.mesh) tracked.set(b, {
        friendly: !!b.friendly,
        type: b.type,
        arcRound: !!b.arcRound,
        napalm: !!b.napalm,
        cluster: !!b.cluster,
        pos: b.mesh.position.clone()
      });
    }

    _updateBullets(dt);

    for(const [b, meta] of tracked.entries()){
      if(state.bullets.includes(b)) continue;
      if(!meta.friendly) continue;
      if(meta.arcRound) dischargeArc(meta.pos.clone());
      if(meta.napalm) spawnNapalmPool(meta.pos.clone());
      if(meta.cluster) spawnClusterShards(meta.pos.clone());
    }
  };

  const _updateHazards = updateHazards;
  updateHazards = function(dt){
    _updateHazards(dt);
    for(let i=state.hazards.length-1;i>=0;i--){
      const h = state.hazards[i];
      if(h.kind !== "napalm") continue;
      h.life -= dt;
      h.pulse += dt;
      if(h.core){
        h.core.material.opacity = 0.14 + Math.sin(h.pulse * 10) * 0.05;
        h.core.scale.setScalar(1 + Math.sin(h.pulse * 6) * 0.08);
      }
      if(h.mesh){
        h.mesh.rotation.z += dt * 0.45;
        h.mesh.material.opacity = 0.24 + Math.sin(h.pulse * 8) * 0.08;
        h.mesh.scale.setScalar(1 + Math.sin(h.pulse * 5) * 0.05);
      }
      for(let j=state.enemies.length-1;j>=0;j--){
        const e = state.enemies[j];
        if(!e?.mesh) continue;
        const dist = e.mesh.position.distanceTo(h.mesh.position);
        if(dist < h.radius){
          e.hp -= h.damage * dt;
          if(Math.random() < dt * 9){
            createBurst(e.mesh.position.clone().add(new THREE.Vector3(0,1.0,0)), 0xffa866, 3, 1.5, { minLife:.08, maxLife:.16, gravity:0.4, shrink:0.95 });
          }
          if(e.hp <= 0){
            killEnemy(e);
            state.enemies.splice(j,1);
          }
        }
      }
      if(state.boss?.mesh && state.boss.mesh.position.distanceTo(h.mesh.position) < h.radius + 0.8){
        state.boss.hp -= h.damage * dt * 0.65;
        updateBossBar();
        if(state.boss.hp <= 0) killEnemy(state.boss);
      }
      if(h.life <= 0){
        scene.remove(h.mesh);
        if(h.core) scene.remove(h.core);
        state.hazards.splice(i,1);
      }
    }
  };

  function pickBossProfile(){
    const idx = Math.max(0, Math.floor(player.wave / 4) - 1) % director.bossProfiles.length;
    return director.bossProfiles[idx];
  }

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss=false){
    const enemy = _spawnEnemy(isBoss);
    if(!enemy) return enemy;

    if(isBoss){
      const profile = pickBossProfile();
      enemy.bossProfile = profile.id;
      enemy.bossLabel = profile.label;
      enemy.maxHp = Math.round(enemy.maxHp * profile.hpMul);
      enemy.hp = enemy.maxHp;
      enemy.speed *= profile.speedMul;
      enemy.specials = {
        profile,
        pulseCd: profile.id === "warden" ? 4.0 : 999,
        summonCd: profile.id === "warden" ? 8.8 : 999,
        cloakCd: profile.id === "phantom" ? 5.0 : 999,
        mirrorCd: profile.id === "phantom" ? 3.2 : 999,
        mortarCd: profile.id === "siegebreaker" ? 4.8 : 999,
        missileCd: profile.id === "siegebreaker" ? 7.2 : 999,
        cloakTime: 0
      };
      tintMesh(enemy.mesh, profile.tint);
      if(enemy.groundRing?.material?.color) enemy.groundRing.material.color.setHex(profile.ringColor);
      if(enemy.groundRing) enemy.groundRing.scale.setScalar(profile.id === "warden" ? 1.18 : profile.id === "phantom" ? 1.08 : 1.14);
      director.lastBossName = `${profile.label} · ${profile.threat}`;
      director.waveDirective = profile.directive;
      if(bossBarLabelEl) bossBarLabelEl.textContent = profile.label.toUpperCase();
      showFloating(profile.intro);
      createFlash(enemy.mesh.position.clone().add(new THREE.Vector3(0,3.4,0)), profile.tint, 3.4, 12, 0.18);
    } else if(player.wave >= 6 && enemy.type === "elite"){
      enemy.speed *= 1.05;
      enemy.hp *= 1.08;
      tintMesh(enemy.mesh, 0xffb26b);
    }
    return enemy;
  };

  function spawnBossMortarField(center, amount=3, radius=6.5, color=0xffae6b){
    for(let i=0;i<amount;i++){
      const angle = Math.random() * Math.PI * 2;
      const dist = 1.6 + Math.random() * radius;
      const pos = center.clone().add(new THREE.Vector3(Math.cos(angle) * dist, 0.05, Math.sin(angle) * dist));
      const marker = new THREE.Mesh(
        new THREE.RingGeometry(0.9, 1.45, 28),
        new THREE.MeshBasicMaterial({ color, transparent:true, opacity:0.72, side:THREE.DoubleSide })
      );
      marker.rotation.x = -Math.PI / 2;
      marker.position.copy(pos);
      scene.add(marker);
      setTimeout(() => scene.remove(marker), 900);
      setTimeout(() => {
        if(state.running && player.alive){
          explodeAt(pos.clone(), 3.1, 14, color);
        }
      }, 850);
    }
  }

  const _updateEnemies = updateEnemies;
  updateEnemies = function(dt){
    _updateEnemies(dt);

    const boss = state.boss;
    if(boss?.specials?.profile){
      const s = boss.specials;
      s.pulseCd -= dt;
      s.summonCd -= dt;
      s.cloakCd -= dt;
      s.mirrorCd -= dt;
      s.mortarCd -= dt;
      s.missileCd -= dt;
      if(s.cloakTime > 0) s.cloakTime -= dt;

      if(s.profile.id === "warden"){
        if(s.pulseCd <= 0){
          s.pulseCd = boss.phase >= 3 ? 3.8 : 5.1;
          createShockwave(boss.mesh.position.clone(), 0x7ec8ff, boss.phase >= 3 ? 7.2 : 5.4);
          for(const enemy of state.enemies){
            if(enemy?.mesh && enemy.mesh.position.distanceTo(boss.mesh.position) < 12){
              enemy.hp = Math.min(enemy.maxHp, enemy.hp + 8);
            }
          }
          if(player.pos.distanceTo(boss.mesh.position) < (boss.phase >= 3 ? 7.0 : 5.5)){
            applyDamage(boss.phase >= 3 ? 17 : 11);
          }
          showFloating("WARDEN PULSE");
        }
        if(s.summonCd <= 0 && state.enemies.length < 16){
          s.summonCd = boss.phase >= 3 ? 7.0 : 9.2;
          for(let i=0;i<(boss.phase >= 3 ? 2 : 1);i++){
            const guard = spawnEnemy(false);
            if(guard){
              guard.type = i === 0 ? "tank" : guard.type;
              guard.hp *= 1.15;
              guard.maxHp = guard.hp;
              tintMesh(guard.mesh, 0x7ec8ff);
            }
          }
          showFloating("WARDEN GUARDS");
        }
      } else if(s.profile.id === "phantom"){
        if(s.cloakCd <= 0){
          s.cloakCd = boss.phase >= 3 ? 4.0 : 5.4;
          s.cloakTime = boss.phase >= 3 ? 1.4 : 1.0;
        }
        const cloakAlpha = s.cloakTime > 0 ? 0.28 : 1;
        boss.mesh.traverse?.(obj => {
          if(!obj.material) return;
          const mats = Array.isArray(obj.material) ? obj.material : [obj.material];
          mats.forEach(mat => {
            mat.transparent = cloakAlpha < 1;
            mat.opacity = cloakAlpha;
          });
        });
        if(s.mirrorCd <= 0){
          s.mirrorCd = boss.phase >= 3 ? 2.2 : 3.1;
          const from = boss.mesh.position.clone().add(new THREE.Vector3(0,2.5,0));
          const target = player.pos.clone().add(new THREE.Vector3(0,1.3,0));
          const baseDir = target.sub(from).normalize();
          const right = new THREE.Vector3(baseDir.z, 0, -baseDir.x).normalize();
          [-0.22, 0, 0.22].forEach(offset => {
            const d = baseDir.clone().addScaledVector(right, offset).normalize();
            state.enemyBullets.push(_createProjectile(from.clone(), d, {
              speed: 19,
              friendly:false,
              color: 0xff7fcf,
              size: 0.17,
              life: 2.8,
              damage: boss.phase >= 3 ? 16 : 12,
              type:"enemy"
            }));
          });
          createFlash(from.clone(), 0xff7fcf, 2.2, 8, 0.10);
        }
        if(s.cloakTime > 0.5 && boss.dashCooldown <= 0){
          boss.dashCooldown = boss.phase >= 3 ? 2.6 : 3.4;
          const dx = player.pos.x - boss.mesh.position.x;
          const dz = player.pos.z - boss.mesh.position.z;
          const dist = Math.hypot(dx, dz) || 1;
          const tx = boss.mesh.position.x + (dx / dist) * 5.4;
          const tz = boss.mesh.position.z + (dz / dist) * 5.4;
          if(!collidesAt(tx, tz, boss.radius)){
            boss.mesh.position.x = tx;
            boss.mesh.position.z = tz;
            createShockwave(boss.mesh.position.clone(), 0xff7fcf, 4.2);
            if(player.pos.distanceTo(boss.mesh.position) < 4.1) applyDamage(boss.phase >= 3 ? 15 : 10);
          }
        }
      } else if(s.profile.id === "siegebreaker"){
        if(s.mortarCd <= 0){
          s.mortarCd = boss.phase >= 3 ? 3.4 : 4.8;
          spawnBossMortarField(player.pos.clone(), boss.phase >= 3 ? 4 : 3, boss.phase >= 3 ? 8.5 : 6.5, 0xffc86f);
          showFloating("MORTAR LOCK");
        }
        if(s.missileCd <= 0){
          s.missileCd = boss.phase >= 3 ? 5.5 : 7.2;
          const targetBase = player.pos.clone();
          [
            new THREE.Vector3(-3, 0, -3),
            new THREE.Vector3(3, 0, -3),
            new THREE.Vector3(-3, 0, 3),
            new THREE.Vector3(3, 0, 3)
          ].forEach((off, idx) => {
            setTimeout(() => {
              if(!state.running || !player.alive || state.boss !== boss) return;
              const pos = targetBase.clone().add(off);
              spawnBossMortarField(pos, 1, 0.1, 0xff8c55);
            }, idx * 140);
          });
          createFlash(boss.mesh.position.clone().add(new THREE.Vector3(0,3.2,0)), 0xffc86f, 3.0, 11, 0.15);
        }
      }
    }

    refreshDirectorHud();
  };

  const _spawnWave = spawnWave;
  spawnWave = function(){
    _spawnWave();
    director.waveDirective =
      player.wave % 4 === 0 ? "Boss wave: houd lanes vrij en gebruik skills reactief." :
      player.wave >= 10 ? "Hoog tempo: chain kills voor combo-control en emergency ammo voorkomen." :
      player.wave >= 6 ? "Elite packs actief: speel rond cover pods en forceer sightlines." :
      "Bouw resources op en leer de sector-routes.";

    if(state.preBossRelief && player.wave % 5 === 0){
      state.preBossRelief = false;
      setTimeout(() => {
        if(!state.running || !player.alive) return;
        const trimCount = Math.min(3, state.enemies.length);
        for(let i = 0; i < trimCount; i++){
          const target = state.enemies[state.enemies.length - 1];
          if(target) damageEnemyDirect?.(target, 999999);
        }
        showFloating("BOSS ENTRY STABILIZED");
      }, 700);
    }

    if(player.wave >= 5 && player.wave % 3 === 2){
      setTimeout(() => {
        if(!state.running || !player.alive) return;
        const extra = Math.min(2 + Math.floor(player.wave / 6), 4);
        for(let i=0;i<extra;i++){
          const e = spawnEnemy(false);
          if(e){
            e.hp *= 1.06;
            e.maxHp = e.hp;
          }
        }
        showFloating("REINFORCEMENT PACK");
      }, 1900);
    }
    refreshDirectorHud(true);
  };

  const _applyDamage = applyDamage;
  applyDamage = function(amount){
    _applyDamage(amount);
    refreshDirectorHud(true);
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    if(enemy?.bossLabel){
      director.lastBossName = `${enemy.bossLabel} verslagen`;
      if(bossBarLabelEl) bossBarLabelEl.textContent = "BOSS";
      director.waveDirective = "Arena opgeschoond. Herpositioneer en pak loot voordat de volgende golf start.";
    }
    return _killEnemy(enemy);
  };

  refreshDirectorHud(true);
})();


/* ===========================
   OH V2 COMBAT POLISH PACK
   toegevoegd voor reloads / heat / unlocks / boss archetypes
   =========================== */
(() => {
  const V2_KEY = "oldehanter_combat_polish_v2";
  const combat = {
    profile: loadCombatProfile(),
    shotsInMag: { bullet:0, rocket:0, grenade:0 },
    totalShots: { bullet:0, rocket:0, grenade:0 },
    heat: 0,
    overheat: false,
    overheatLock: 0,
    reload: null,
    arcCounter: 0,
    runBossKills: 0,
    hud: {}
  };

  function loadCombatProfile(){
    try{
      const raw = JSON.parse(localStorage.getItem(V2_KEY) || "null") || {};
      return {
        totalBossKills: raw.totalBossKills || 0,
        bestWave: raw.bestWave || 1,
        bestScore: raw.bestScore || 0,
        totalRuns: raw.totalRuns || 0,
        unlocks: {
          ventedBarrel: !!raw.unlocks?.ventedBarrel,
          expandedMags: !!raw.unlocks?.expandedMags,
          fieldLoader: !!raw.unlocks?.fieldLoader
        }
      };
    }catch{
      return {
        totalBossKills: 0,
        bestWave: 1,
        bestScore: 0,
        totalRuns: 0,
        unlocks: { ventedBarrel:false, expandedMags:false, fieldLoader:false }
      };
    }
  }

  function saveCombatProfile(){
    try{ localStorage.setItem(V2_KEY, JSON.stringify(combat.profile)); }catch{}
  }

  function cClamp(v,a,b){ return Math.max(a, Math.min(b, v)); }
  function cFmt(n){ return Math.round(n || 0).toLocaleString("nl-NL"); }
  function weaponText(w){ return w === "bullet" ? "Bullet" : w === "rocket" ? "Rocket" : "Grenade"; }
  function magBase(w){ return w === "bullet" ? 18 : 1; }
  function getMagSize(w){
    const bonus = combat.profile.unlocks.expandedMags ? (w === "bullet" ? 6 : 1) : 0;
    return magBase(w) + bonus;
  }
  function getReloadTime(w){
    const base = w === "bullet" ? 1.12 : w === "rocket" ? 1.52 : 1.42;
    return Math.max(0.55, base - (combat.profile.unlocks.fieldLoader ? 0.18 : 0));
  }

  function finalizeCombatRun(){
    combat.profile.bestWave = Math.max(combat.profile.bestWave || 1, player.wave || 1);
    combat.profile.bestScore = Math.max(combat.profile.bestScore || 0, Math.floor(player.score || 0));
    refreshUnlocks();
    saveCombatProfile();
    refreshCombatHud();
  }

  function refreshUnlocks(){
    const u = combat.profile.unlocks;
    let changed = false;
    if(!u.ventedBarrel && combat.profile.totalBossKills >= 2){ u.ventedBarrel = true; changed = true; }
    if(!u.expandedMags && combat.profile.totalBossKills >= 4){ u.expandedMags = true; changed = true; }
    if(!u.fieldLoader && combat.profile.bestWave >= 8){ u.fieldLoader = true; changed = true; }
    if(changed){
      showFloating?.("ARSENAL UPGRADE ONLINE");
      flashHint?.("Nieuwe combat unlock vrijgespeeld", 1800);
    }
  }

  function addCombatStyles(){
    if(document.getElementById("ohCombatPolishStyles")) return;
    const style = document.createElement("style");
    style.id = "ohCombatPolishStyles";
    style.textContent = `
      #combatPolishHud{
        position:fixed;
        left:16px;
        top:16px;
        z-index:28;
        width:min(360px, calc(100vw - 32px));
        display:flex;
        flex-direction:column;
        gap:10px;
        pointer-events:none;
      }
      .combat-card{
        pointer-events:auto;
        border-radius:18px;
        padding:12px 14px;
        color:#fff;
        background:linear-gradient(180deg, rgba(6,10,20,.84), rgba(12,8,28,.74));
        border:1px solid rgba(255,255,255,.12);
        box-shadow:0 16px 34px rgba(0,0,0,.30), 0 0 28px rgba(0,247,255,.10);
        backdrop-filter:blur(14px) saturate(1.08);
      }
      .combat-head{
        display:flex;
        align-items:center;
        justify-content:space-between;
        gap:10px;
        font-weight:900;
      }
      .combat-kicker{
        font-size:.76rem;
        letter-spacing:.15em;
        text-transform:uppercase;
        color:rgba(255,255,255,.68);
      }
      .combat-badge{
        display:inline-flex;
        align-items:center;
        gap:8px;
        padding:.34rem .7rem;
        border-radius:999px;
        background:rgba(255,255,255,.08);
        border:1px solid rgba(255,255,255,.12);
        font-weight:800;
      }
      .combat-grid{
        display:grid;
        grid-template-columns:1.2fr .8fr;
        gap:12px;
        margin-top:12px;
      }
      .combat-label{
        font-size:.78rem;
        letter-spacing:.12em;
        text-transform:uppercase;
        color:rgba(255,255,255,.58);
        font-weight:900;
      }
      .combat-value{
        margin-top:3px;
        font-size:1.02rem;
        font-weight:900;
      }
      .combat-meter{
        height:12px;
        margin-top:8px;
        border-radius:999px;
        overflow:hidden;
        border:1px solid rgba(255,255,255,.12);
        background:rgba(255,255,255,.07);
      }
      .combat-meter > i{
        display:block;
        height:100%; width:0%;
        background:linear-gradient(90deg,#00f7ff,#8a2eff,#ff00a8,#ffe600);
        background-size:220% 220%;
        animation:combatFlow 4s linear infinite;
        box-shadow:0 0 18px rgba(0,247,255,.18);
      }
      .combat-meter.heat > i{
        background:linear-gradient(90deg,#5cff8d,#ffe600,#ff8c55,#ff3d7d);
      }
      .combat-note{
        margin-top:10px;
        color:rgba(255,255,255,.74);
        font-size:.84rem;
        font-weight:700;
        line-height:1.38;
      }
      .combat-note.hot{ color:#ffd166; }
      .combat-note.warn{ color:#ff8da8; }
      .combat-mini{
        margin-top:10px;
        display:grid;
        grid-template-columns:repeat(3,minmax(0,1fr));
        gap:8px;
      }
      .combat-pill{
        border-radius:12px;
        padding:.58rem .64rem;
        background:rgba(255,255,255,.06);
        border:1px solid rgba(255,255,255,.08);
        font-size:.78rem;
        font-weight:800;
        line-height:1.3;
      }
      #bossIntroBanner{
        position:fixed;
        left:50%; top:20%;
        transform:translateX(-50%);
        z-index:58;
        min-width:min(760px, calc(100vw - 24px));
        max-width:calc(100vw - 24px);
        padding:16px 18px;
        border-radius:22px;
        background:linear-gradient(90deg, rgba(255,0,168,.18), rgba(10,10,26,.92), rgba(0,247,255,.18));
        border:1px solid rgba(255,255,255,.15);
        box-shadow:0 22px 50px rgba(0,0,0,.34), 0 0 28px rgba(255,0,168,.16);
        text-align:center;
        color:#fff;
        backdrop-filter:blur(14px) saturate(1.08);
        opacity:0;
        pointer-events:none;
        transition:opacity .24s ease, transform .24s ease;
      }
      #bossIntroBanner.show{ opacity:1; transform:translateX(-50%) translateY(0); }
      #bossIntroBanner .sub{
        font-size:.8rem;
        letter-spacing:.22em;
        text-transform:uppercase;
        color:rgba(255,255,255,.68);
        font-weight:900;
      }
      #bossIntroBanner .title{
        margin-top:6px;
        font-size:clamp(1.3rem, 3vw, 2.1rem);
        font-weight:900;
      }
      #bossIntroBanner .desc{
        margin-top:4px;
        color:rgba(255,255,255,.78);
        font-weight:700;
      }
      @keyframes combatFlow{ 0%{background-position:0% 50%;} 100%{background-position:220% 50%;} }
      @media (max-width: 920px){
        #combatPolishHud{ left:10px; top:auto; bottom:138px; width:min(310px, calc(100vw - 20px)); }
        .combat-grid{ grid-template-columns:1fr; }
        .combat-mini{ grid-template-columns:1fr 1fr 1fr; }
      }
    `;
    document.head.appendChild(style);
  }

  function buildCombatHud(){
    addCombatStyles();
    let root = document.getElementById("combatPolishHud");
    if(!root){
      root = document.createElement("div");
      root.id = "combatPolishHud";
      root.innerHTML = `
        <div class="combat-card">
          <div class="combat-kicker">Combat polish v2</div>
          <div class="combat-head">
            <div class="combat-badge">Mag <b id="combatMagText">18 / 18</b></div>
            <div class="combat-badge">Heat <b id="combatHeatText">0%</b></div>
          </div>
          <div class="combat-grid">
            <div>
              <div class="combat-label">Weapon status</div>
              <div class="combat-value" id="combatWeaponText">Bullet · Ready</div>
              <div class="combat-meter"><i id="combatReloadBar"></i></div>
            </div>
            <div>
              <div class="combat-label">Thermal load</div>
              <div class="combat-value" id="combatStateText">Stable</div>
              <div class="combat-meter heat"><i id="combatHeatBar"></i></div>
            </div>
          </div>
          <div class="combat-note" id="combatHint">R herlaadt · hou vuur onder controle om overheat te vermijden.</div>
          <div class="combat-mini">
            <div class="combat-pill">Boss kills<br><b id="combatBossKills">0</b></div>
            <div class="combat-pill">Best wave<br><b id="combatBestWave">1</b></div>
            <div class="combat-pill">Best score<br><b id="combatBestScore">0</b></div>
          </div>
        </div>`;
      document.body.appendChild(root);
    }

    let banner = document.getElementById("bossIntroBanner");
    if(!banner){
      banner = document.createElement("div");
      banner.id = "bossIntroBanner";
      banner.innerHTML = `<div class="sub">Boss archetype</div><div class="title"></div><div class="desc"></div>`;
      document.body.appendChild(banner);
    }

    combat.hud = {
      root,
      magText: root.querySelector("#combatMagText"),
      heatText: root.querySelector("#combatHeatText"),
      weaponText: root.querySelector("#combatWeaponText"),
      reloadBar: root.querySelector("#combatReloadBar"),
      heatBar: root.querySelector("#combatHeatBar"),
      stateText: root.querySelector("#combatStateText"),
      hint: root.querySelector("#combatHint"),
      bossKills: root.querySelector("#combatBossKills"),
      bestWave: root.querySelector("#combatBestWave"),
      bestScore: root.querySelector("#combatBestScore"),
      banner
    };
    refreshCombatHud();
  }

  function showBossBanner(title, desc){
    const el = combat.hud.banner;
    if(!el) return;
    el.querySelector(".title").textContent = title;
    el.querySelector(".desc").textContent = desc;
    el.classList.add("show");
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => el.classList.remove("show"), 2200);
  }

  function refreshCombatHud(){
    if(!combat.hud.root) return;
    const w = player.weapon || "bullet";
    const magSize = getMagSize(w);
    const used = cClamp(combat.shotsInMag[w] || 0, 0, magSize);
    const left = Math.max(0, magSize - used);
    combat.hud.magText.textContent = `${left} / ${magSize}`;
    combat.hud.heatText.textContent = `${Math.round(combat.heat)}%`;
    combat.hud.heatBar.style.width = `${cClamp(combat.heat, 0, 100)}%`;

    let weaponState = `${weaponText(w)} · Ready`;
    let hint = "R herlaadt · hou vuur onder controle om overheat te vermijden.";
    combat.hud.hint.classList.remove("hot", "warn");

    if(combat.reload && combat.reload.weapon === w){
      const pct = 100 * (1 - cClamp(combat.reload.time / combat.reload.max, 0, 1));
      combat.hud.reloadBar.style.width = `${pct}%`;
      weaponState = `${weaponText(w)} · Reloading`;
      hint = `${weaponText(w)} herlaadt...`; 
    }else{
      const pct = 100 * (1 - used / Math.max(1, magSize));
      combat.hud.reloadBar.style.width = `${pct}%`;
      if(left <= Math.ceil(magSize * 0.25)){
        hint = "Laag magazijn — herlaad tussen pushes.";
        combat.hud.hint.classList.add("hot");
      }
    }

    let thermalState = combat.overheat ? "Overheated" : combat.heat > 72 ? "Hot" : combat.heat > 38 ? "Warming" : "Stable";
    if(combat.overheat){
      hint = "Overheat lock actief — repositioneer en laat heat zakken.";
      combat.hud.hint.classList.add("warn");
    }

    combat.hud.weaponText.textContent = weaponState;
    combat.hud.stateText.textContent = thermalState;
    combat.hud.hint.textContent = hint;
    combat.hud.bossKills.textContent = cFmt(combat.profile.totalBossKills);
    combat.hud.bestWave.textContent = cFmt(combat.profile.bestWave);
    combat.hud.bestScore.textContent = cFmt(combat.profile.bestScore);
  }

  function resetCombatRun(){
    combat.shotsInMag = { bullet:0, rocket:0, grenade:0 };
    combat.totalShots = { bullet:0, rocket:0, grenade:0 };
    combat.heat = 0;
    combat.overheat = false;
    combat.overheatLock = 0;
    combat.reload = null;
    combat.arcCounter = 0;
    combat.runBossKills = 0;
    refreshCombatHud();
  }

  function startReload(weapon, silent=false){
    if(!player.alive) return false;
    if(!(weapon in combat.shotsInMag)) return false;
    if((player.ammo[weapon] || 0) <= 0) return false;
    if(combat.reload && combat.reload.weapon === weapon) return false;
    if((combat.shotsInMag[weapon] || 0) <= 0 && !silent) return false;
    combat.reload = {
      weapon,
      time: getReloadTime(weapon),
      max: getReloadTime(weapon)
    };
    player.fireCooldown = Math.max(player.fireCooldown || 0, weapon === "bullet" ? 0.22 : 0.34);
    if(!silent){
      showFloating?.(`${weaponText(weapon)} reload`);
      flashHint?.(`${weaponText(weapon)} herladen`, 900);
    }
    refreshCombatHud();
    return true;
  }

  function addHeatForWeapon(weapon){
    const add = weapon === "bullet" ? 7.8 : weapon === "rocket" ? 18 : 13;
    const vent = combat.profile.unlocks.ventedBarrel ? 0.84 : 1;
    combat.heat = Math.min(100, combat.heat + add * vent);
    if(combat.heat >= 100 && !combat.overheat){
      combat.overheat = true;
      combat.overheatLock = combat.profile.unlocks.ventedBarrel ? 1.05 : 1.35;
      player.fireCooldown = Math.max(player.fireCooldown || 0, combat.overheatLock);
      state.cameraShake = Math.min(2.1, (state.cameraShake || 0) + 0.28);
      createShockwave?.(player.pos.clone(), 0xff8c55, 2.1);
      showFloating?.("WEAPON OVERHEAT");
    }
  }

  function findForwardTarget(maxDist=26){
    const from = player.pos.clone();
    from.y = 1.52;
    const dir = new THREE.Vector3();
    camera.getWorldDirection(dir);
    dir.normalize();
    let best = null;
    let bestDot = 0.94;
    const candidates = [];
    if(state.boss?.mesh) candidates.push(state.boss);
    for(const e of state.enemies) candidates.push(e);
    for(const enemy of candidates){
      if(!enemy?.mesh) continue;
      const target = enemy.mesh.position.clone();
      target.y += enemy.isBoss ? 2.2 : 1.35;
      const to = target.clone().sub(from);
      const dist = to.length();
      if(dist <= 0.5 || dist > maxDist) continue;
      const nd = to.normalize();
      const dot = dir.dot(nd);
      if(dot > bestDot){
        bestDot = dot;
        best = { enemy, dir: nd, dist };
      }
    }
    return best;
  }

  function applyShotPolish(weapon, dirOverride, beforeCount){
    const added = state.bullets.slice(beforeCount);
    for(const b of added){
      b.v2SourceWeapon = weapon;
      b.v2Friendly = true;
    }

    const dir = dirOverride ? dirOverride.clone().normalize() : new THREE.Vector3();
    if(!dirOverride) camera.getWorldDirection(dir);
    dir.normalize();

    if(weapon === "bullet"){
      combat.arcCounter += 1;
      if(combat.arcCounter % 7 === 0){
        const target = findForwardTarget(30);
        const shotDir = target?.dir?.clone() || dir.clone();
        const start = player.pos.clone();
        start.y = 1.55;
        start.add(shotDir.clone().multiplyScalar(0.92));
        const arc = createProjectile(start, shotDir, {
          speed: 34,
          friendly: true,
          color: 0x8bf0ff,
          trailColor: 0xdffcff,
          size: 0.11,
          life: 1.35,
          damage: 17,
          type: "plasma",
          radius: 1.8,
          explosionColor: 0x8bf0ff
        });
        arc.v2SourceWeapon = "bullet_arc";
        state.bullets.push(arc);
        createFlash?.(start.clone(), 0x8bf0ff, 1.4, 4.5, 0.05);
        flashHint?.("Arc round", 420);
      }
    }
  }

  function spawnNapalmHazard(position){
    const mesh = new THREE.Mesh(
      new THREE.CircleGeometry(1.5, 32),
      new THREE.MeshBasicMaterial({ color:0xff8c55, transparent:true, opacity:0.36, side:THREE.DoubleSide })
    );
    mesh.rotation.x = -Math.PI / 2;
    mesh.position.copy(position);
    mesh.position.y = 0.04;
    scene.add(mesh);
    state.hazards.push({
      kind: "napalm",
      mesh,
      life: 4.8,
      radius: 3.2,
      dps: 18,
      pulse: 0,
      tick: 0
    });
  }

  function spawnClusterBursts(position){
    const offsets = [
      new THREE.Vector3(2.1, 0, 0),
      new THREE.Vector3(-2.1, 0, 0),
      new THREE.Vector3(0, 0, 2.1),
      new THREE.Vector3(0, 0, -2.1)
    ];
    offsets.forEach((off, idx) => {
      setTimeout(() => {
        if(!state.running) return;
        const pos = position.clone().add(off);
        pos.y = 0.25;
        explodeAt(pos, 1.9, 10, 0xc9ff9d);
      }, idx * 65);
    });
  }

  const _updateTimers = updateTimers;
  updateTimers = function(dt){
    _updateTimers(dt);
    const coolBase = combat.profile.unlocks.ventedBarrel ? 29 : 24;
    combat.heat = Math.max(0, combat.heat - coolBase * dt * (player.weapon === "bullet" ? 1.05 : 0.92));
    if(combat.overheatLock > 0){
      combat.overheatLock = Math.max(0, combat.overheatLock - dt);
    }
    if(combat.overheat && combat.overheatLock <= 0 && combat.heat < 34){
      combat.overheat = false;
    }
    if(combat.reload){
      combat.reload.time -= dt;
      if(combat.reload.time <= 0){
        const w = combat.reload.weapon;
        combat.shotsInMag[w] = 0;
        combat.reload = null;
        flashHint?.(`${weaponText(w)} ready`, 650);
      }
    }
    refreshCombatHud();
  };

  const _shootWithDirection = shootWithDirection;
  shootWithDirection = function(dirOverride = null){
    const weapon = player.weapon;
    if(weapon in combat.shotsInMag){
      if(combat.reload && combat.reload.weapon === weapon) return false;
      if(combat.overheat) return false;
      if((combat.shotsInMag[weapon] || 0) >= getMagSize(weapon) && (player.ammo[weapon] || 0) > 0){
        startReload(weapon, true);
        return false;
      }
    }

    const beforeCount = state.bullets.length;
    const ok = _shootWithDirection(dirOverride);
    if(ok && weapon in combat.shotsInMag){
      combat.shotsInMag[weapon] += 1;
      combat.totalShots[weapon] += 1;
      addHeatForWeapon(weapon);
      applyShotPolish(weapon, dirOverride, beforeCount);
      if((combat.shotsInMag[weapon] || 0) >= getMagSize(weapon) && (player.ammo[weapon] || 0) > 0){
        startReload(weapon, true);
      }
      refreshCombatHud();
    }
    return ok;
  };

  const _explodeAt = explodeAt;
  explodeAt = function(position, radius, damage, color){
    let sourceKind = "";
    for(const b of state.bullets){
      if(!b?.v2Friendly || !b?.mesh || !b.v2SourceWeapon) continue;
      if(b.mesh.position.distanceTo(position) < 1.2){
        sourceKind = b.v2SourceWeapon;
        break;
      }
    }

    const result = _explodeAt(position, radius, damage, color);

    if(sourceKind === "rocket"){
      spawnNapalmHazard(position.clone());
    }else if(sourceKind === "grenade"){
      spawnClusterBursts(position.clone());
    }

    return result;
  };

  const _updateHazards = updateHazards;
  updateHazards = function(dt){
    _updateHazards(dt);
    for(let i = state.hazards.length - 1; i >= 0; i--){
      const h = state.hazards[i];
      if(h.kind !== "napalm") continue;
      h.pulse += dt;
      h.tick -= dt;
      h.mesh.scale.setScalar(1 + Math.sin(h.pulse * 5) * 0.08);
      h.mesh.material.opacity = 0.26 + Math.sin(h.pulse * 7) * 0.09;
      if(h.tick <= 0){
        h.tick = 0.18;
        for(let j = state.enemies.length - 1; j >= 0; j--){
          const e = state.enemies[j];
          if(!e?.mesh) continue;
          const dist = e.mesh.position.distanceTo(h.mesh.position);
          if(dist < h.radius){
            e.hp -= h.dps * h.tick;
            createBurst?.(e.mesh.position.clone().add(new THREE.Vector3(0,1.1,0)), 0xff8c55, 3, 1.6, { minLife:.12, maxLife:.22, gravity:0.5, shrink:0.9 });
            if(e.hp <= 0){
              killEnemy(e);
              state.enemies.splice(j, 1);
            }
          }
        }
        if(state.boss?.mesh){
          const dist = state.boss.mesh.position.distanceTo(h.mesh.position);
          if(dist < h.radius){
            state.boss.hp -= h.dps * 0.55;
            updateBossBar?.();
            if(state.boss.hp <= 0) killEnemy(state.boss);
          }
        }
      }
    }
  };

  const BOSS_ARCHETYPES = [
    {
      id: "juggernaut",
      title: "Juggernaut Prime",
      desc: "Zware close-range boss met mortar pulse en front pressure.",
      tint: 0xffb36c,
      apply(enemy){
        enemy.hp *= 1.28; enemy.maxHp = enemy.hp;
        enemy.speed *= 0.94;
        enemy.v2SpecialCd = 4.6;
      },
      update(enemy, dt){
        enemy.v2SpecialCd -= dt;
        if(enemy.v2SpecialCd <= 0){
          enemy.v2SpecialCd = enemy.phase >= 3 ? 3.5 : 4.8;
          spawnBossMortarField?.(enemy.mesh.position.clone(), enemy.phase >= 3 ? 4 : 3, enemy.phase >= 3 ? 7.8 : 6.2, 0xffb36c);
          createShockwave?.(enemy.mesh.position.clone(), 0xffb36c, enemy.phase >= 3 ? 5.2 : 4.2);
          showFloating?.("JUGGERNAUT PULSE");
        }
      }
    },
    {
      id: "phantom",
      title: "Phantom Lancer",
      desc: "Mobiele boss met reposition dash en precision punish.",
      tint: 0x8bf0ff,
      apply(enemy){
        enemy.hp *= 1.08; enemy.maxHp = enemy.hp;
        enemy.speed *= 1.16;
        enemy.v2SpecialCd = 5.2;
      },
      update(enemy, dt){
        enemy.v2SpecialCd -= dt;
        if(enemy.v2SpecialCd <= 0){
          enemy.v2SpecialCd = enemy.phase >= 3 ? 3.2 : 4.4;
          const offset = new THREE.Vector3(Math.random() < 0.5 ? -4.8 : 4.8, 0, Math.random() < 0.5 ? -4.8 : 4.8);
          const target = player.pos.clone().add(offset);
          if(!collidesAt(target.x, target.z, enemy.radius)){
            enemy.mesh.position.x = target.x;
            enemy.mesh.position.z = target.z;
            createShockwave?.(enemy.mesh.position.clone(), 0x8bf0ff, 4.0);
            for(let i=0;i<(enemy.phase >= 3 ? 3 : 2);i++){
              setTimeout(() => {
                if(state.running && player.alive && state.boss === enemy) enemyShoot?.(enemy);
              }, i * 95);
            }
            showFloating?.("PHANTOM SHIFT");
          }
        }
      }
    },
    {
      id: "warlord",
      title: "Warlord Director",
      desc: "Command boss die escorts oproept en lanes dichtzet.",
      tint: 0xff6ea1,
      apply(enemy){
        enemy.hp *= 1.18; enemy.maxHp = enemy.hp;
        enemy.speed *= 1.02;
        enemy.v2SpecialCd = 6.0;
      },
      update(enemy, dt){
        enemy.v2SpecialCd -= dt;
        if(enemy.v2SpecialCd <= 0){
          enemy.v2SpecialCd = enemy.phase >= 3 ? 4.0 : 5.6;
          for(let i = 0; i < (enemy.phase >= 3 ? 3 : 2); i++){
            const add = spawnEnemy?.(false);
            if(add){
              add.hp *= 1.10;
              add.maxHp = add.hp;
            }
          }
          spawnBossMortarField?.(player.pos.clone(), enemy.phase >= 3 ? 2 : 1, enemy.phase >= 3 ? 4.4 : 3.2, 0xff6ea1);
          createFlash?.(enemy.mesh.position.clone().add(new THREE.Vector3(0,3.2,0)), 0xff6ea1, 3.2, 12, 0.14);
          showFloating?.("WARLORD COMMAND");
        }
      }
    }
  ];

  const _spawnEnemy = spawnEnemy;
  spawnEnemy = function(isBoss = false){
    const enemy = _spawnEnemy(isBoss);
    if(isBoss && enemy){
      const profile = BOSS_ARCHETYPES[Math.floor(Math.random() * BOSS_ARCHETYPES.length)];
      enemy.v2BossProfile = profile;
      enemy.bossLabel = profile.title;
      profile.apply?.(enemy);
      if(enemy.groundRing?.material) enemy.groundRing.material.color.setHex(profile.tint);
      const bossLabel = document.getElementById("bossBarLabel");
      if(bossLabel) bossLabel.textContent = profile.title.toUpperCase();
      showBossBanner(profile.title, profile.desc);
    }
    return enemy;
  };

  const _updateEnemies = updateEnemies;
  updateEnemies = function(dt){
    _updateEnemies(dt);
    const boss = state.boss;
    if(boss?.v2BossProfile){
      boss.v2BossProfile.update?.(boss, dt);
    }
    refreshCombatHud();
  };

  const _killEnemy = killEnemy;
  killEnemy = function(enemy){
    if(enemy?.isBoss){
      combat.runBossKills += 1;
      combat.profile.totalBossKills = (combat.profile.totalBossKills || 0) + 1;
      refreshUnlocks();
      saveCombatProfile();
    }
    return _killEnemy(enemy);
  };

  const _applyDamage = applyDamage;
  applyDamage = function(amount){
    const wasAlive = player.alive;
    const result = _applyDamage(amount);
    if(wasAlive && !player.alive){
      finalizeCombatRun();
    }
    return result;
  };

  const _restartGame = restartGame;
  restartGame = function(){
    finalizeCombatRun();
    resetCombatRun();
    combat.profile.totalRuns = (combat.profile.totalRuns || 0) + 1;
    saveCombatProfile();
    return _restartGame();
  };

  const _startGame = startGame;
  startGame = function(){
    resetCombatRun();
    combat.profile.totalRuns = (combat.profile.totalRuns || 0) + 1;
    saveCombatProfile();
    return _startGame();
  };

  window.addEventListener("keydown", (e) => {
    if((e.code === "KeyR" || e.key === "r" || e.key === "R") && !e.repeat){
      if(player.weapon in combat.shotsInMag){
        e.preventDefault();
        startReload(player.weapon);
      }
    }
  }, { passive:false });

  buildCombatHud();
  refreshUnlocks();
  refreshCombatHud();
})();


/* =========================
   OLDE HANTER FINAL V3 META LOOP / CONTRACTS / LOADOUTS
   plak dit direct boven: animate(performance.now());
   ========================= */
(() => {
  const FINAL_META_KEY = "oldehanter_final_v3_meta";
  const meta = {
    profile: loadMetaProfile(),
    runtime: {
      currentSector: "Core Nexus",
      objective: null,
      currentBossRef: null,
      lastWaveSeen: 0,
      minibossWave: 0,
      minibossAlive: false,
      runWeaponKills: { bullet:0, rocket:0, grenade:0 },
      runDamageAmp: 1,
      cinematicTimer: 0,
      loadoutApplied: false
    }
  };

  function loadMetaProfile(){
    try{
      const saved = JSON.parse(localStorage.getItem(FINAL_META_KEY) || "null") || {};
      return {
        cores: saved.cores || 0,
        schematics: saved.schematics || 0,
        bestWave: saved.bestWave || 1,
        weaponXP: Object.assign({ bullet:0, rocket:0, grenade:0 }, saved.weaponXP || {}),
        weaponLevel: Object.assign({ bullet:1, rocket:1, grenade:1 }, saved.weaponLevel || {}),
        completedContracts: saved.completedContracts || 0,
        completedObjectives: saved.completedObjectives || 0,
        bossContracts: saved.bossContracts || 0,
        totalMinibossKills: saved.totalMinibossKills || 0,
        activeLoadout: saved.activeLoadout || "vanguard",
        loadouts: Object.assign({
          vanguard: {
            label: "Vanguard",
            startWeapon: "bullet",
            bonusHp: 14,
            speed: 0.15,
            ammo: { bullet: 18, rocket: 0, grenade: 0 },
            abilities: { plasma: 1, mine: 0, orbital: 0 },
            perk: "Stabiele opener met extra HP en rifle momentum"
          },
          demolisher: {
            label: "Demolisher",
            startWeapon: "rocket",
            bonusHp: 6,
            speed: -0.1,
            ammo: { bullet: 0, rocket: 2, grenade: 1 },
            abilities: { plasma: 0, mine: 1, orbital: 0 },
            perk: "Explosieve start met rocket pressure en area denial"
          },
          tactician: {
            label: "Tactician",
            startWeapon: "grenade",
            bonusHp: 8,
            speed: 0.08,
            ammo: { bullet: 8, rocket: 1, grenade: 2 },
            abilities: { plasma: 0, mine: 1, orbital: 1 },
            perk: "Utility-heavy run met crowd control en orbital follow-up"
          }
        }, saved.loadouts || {})
      };
    }catch(err){
      return {
        cores: 0,
        schematics: 0,
        bestWave: 1,
        weaponXP: { bullet:0, rocket:0, grenade:0 },
        weaponLevel: { bullet:1, rocket:1, grenade:1 },
        completedContracts: 0,
        completedObjectives: 0,
        bossContracts: 0,
        totalMinibossKills: 0,
        activeLoadout: "vanguard",
        loadouts: {
          vanguard: { label:"Vanguard", startWeapon:"bullet", bonusHp:14, speed:0.15, ammo:{ bullet:18, rocket:0, grenade:0 }, abilities:{ plasma:1, mine:0, orbital:0 }, perk:"Stabiele opener met extra HP en rifle momentum" },
          demolisher: { label:"Demolisher", startWeapon:"rocket", bonusHp:6, speed:-0.1, ammo:{ bullet:0, rocket:2, grenade:1 }, abilities:{ plasma:0, mine:1, orbital:0 }, perk:"Explosieve start met rocket pressure en area denial" },
          tactician: { label:"Tactician", startWeapon:"grenade", bonusHp:8, speed:0.08, ammo:{ bullet:8, rocket:1, grenade:2 }, abilities:{ plasma:0, mine:1, orbital:1 }, perk:"Utility-heavy run met crowd control en orbital follow-up" }
        }
      };
    }
  }

  function saveMetaProfile(){
    try{ localStorage.setItem(FINAL_META_KEY, JSON.stringify(meta.profile)); }catch(err){}
  }

  function xpNeeded(level){ return 70 + (level - 1) * 55; }
  function weaponLabelShort(w){ return w === "bullet" ? "Rifle" : w === "rocket" ? "Rocket" : "Grenade"; }
  function currentLoadout(){ return meta.profile.loadouts[meta.profile.activeLoadout] || meta.profile.loadouts.vanguard; }
  function weaponTreeBonus(weapon){
    const lvl = meta.profile.weaponLevel[weapon] || 1;
    return {
      damage: 1 + Math.max(0, lvl - 1) * 0.06,
      radius: 1 + Math.max(0, lvl - 1) * 0.05,
      speed: 1 + Math.max(0, lvl - 1) * 0.025,
      heat: 1 - Math.max(0, lvl - 1) * 0.03
    };
  }

  function awardWeaponXP(weapon, amount){
    if(!(weapon in meta.profile.weaponXP)) return;
    meta.profile.weaponXP[weapon] += amount;
    let leveled = false;
    while(meta.profile.weaponXP[weapon] >= xpNeeded(meta.profile.weaponLevel[weapon])){
      meta.profile.weaponXP[weapon] -= xpNeeded(meta.profile.weaponLevel[weapon]);
      meta.profile.weaponLevel[weapon] += 1;
      leveled = true;
      flashHint?.(`${weaponLabelShort(weapon)} tree level ${meta.profile.weaponLevel[weapon]}`, 1200);
      showFloating?.(`${weaponLabelShort(weapon).toUpperCase()} UPGRADE`);
      createShockwave?.(player.pos.clone(), weapon === "bullet" ? 0x8bf0ff : weapon === "rocket" ? 0xffb36c : 0xc9ff9d, 3.8);
    }
    if(leveled) saveMetaProfile();
    renderMetaPanel();
  }

  function buildMetaStyles(){
    if(document.getElementById("ohFinalMetaStyles")) return;
    const style = document.createElement("style");
    style.id = "ohFinalMetaStyles";
    style.textContent = `
      #metaHud{
        position:fixed; left:14px; bottom:14px; z-index:26; pointer-events:none;
        background:linear-gradient(180deg, rgba(10,16,30,.88), rgba(10,12,22,.76));
        border:1px solid rgba(133,211,255,.16); border-radius:18px; box-shadow:0 18px 36px rgba(0,0,0,.26);
        padding:12px 14px; min-width:260px; backdrop-filter:blur(10px);
      }
      #metaHud .row{display:flex; justify-content:space-between; gap:12px; margin:4px 0; font-size:12px;}
      #metaHud .title{font-size:12px; font-weight:800; letter-spacing:.1em; text-transform:uppercase; color:#dff6ff; margin-bottom:6px;}
      #metaHud .soft{color:rgba(230,240,255,.72);}
      #metaHud .accent{color:#8cecff; font-weight:700;}
      #metaPanelWrap{
        position:fixed; inset:0; z-index:30; display:none; align-items:center; justify-content:center;
        background:rgba(2,4,10,.56); backdrop-filter:blur(8px);
      }
      #metaPanel{
        width:min(980px, calc(100vw - 28px)); max-height:min(86vh, 860px); overflow:auto;
        border-radius:24px; border:1px solid rgba(145,214,255,.14);
        background:linear-gradient(180deg, rgba(11,18,36,.97), rgba(8,12,24,.97));
        box-shadow:0 30px 70px rgba(0,0,0,.42); padding:22px;
      }
      #metaPanel h2{margin:0 0 6px; font-size:24px;}
      #metaPanel .sub{color:rgba(227,242,255,.72); margin-bottom:16px;}
      #metaPanel .grid{display:grid; grid-template-columns:1.2fr 1fr; gap:16px;}
      #metaPanel .stack{display:grid; gap:14px;}
      #metaPanel .card{border-radius:18px; border:1px solid rgba(255,255,255,.08); background:rgba(255,255,255,.04); padding:14px;}
      #metaPanel .card h3{margin:0 0 10px; font-size:14px; letter-spacing:.08em; text-transform:uppercase; color:#e6f6ff;}
      #metaPanel .pill{display:inline-flex; align-items:center; gap:8px; border-radius:999px; border:1px solid rgba(255,255,255,.08); background:rgba(255,255,255,.05); padding:8px 12px; font-size:12px; margin:4px 6px 0 0;}
      #metaPanel .loadouts, #metaPanel .weapons{display:grid; gap:10px;}
      #metaPanel .loadout, #metaPanel .weapon{border-radius:16px; border:1px solid rgba(255,255,255,.08); background:rgba(255,255,255,.03); padding:12px;}
      #metaPanel .loadout.active, #metaPanel .weapon.active{border-color:rgba(113,230,255,.42); box-shadow:0 0 0 1px rgba(113,230,255,.18) inset;}
      #metaPanel .topline{display:flex; justify-content:space-between; gap:12px; align-items:flex-start;}
      #metaPanel .name{font-weight:800; font-size:15px;}
      #metaPanel .muted{color:rgba(226,240,255,.74); font-size:12px; line-height:1.45;}
      #metaPanel button{border:0; cursor:pointer; border-radius:12px; padding:10px 12px; font-weight:700; color:#05101a; background:linear-gradient(180deg, #9fe8ff, #61d0ff);}
      #metaPanel button.alt{background:rgba(255,255,255,.08); color:#e8f9ff; border:1px solid rgba(255,255,255,.08);}
      #metaPanel .btns{display:flex; flex-wrap:wrap; gap:8px; margin-top:10px;}
      #metaPanel .bar{height:8px; border-radius:999px; background:rgba(255,255,255,.08); overflow:hidden; margin-top:8px;}
      #metaPanel .bar > i{display:block; height:100%; border-radius:inherit; background:linear-gradient(90deg, #7fd8ff, #96ffd4);}
      #bossCinematic{
        position:fixed; inset:0; z-index:28; pointer-events:none; display:none; align-items:center; justify-content:center;
        background:radial-gradient(circle at center, rgba(255,255,255,.04), rgba(2,4,10,.72));
      }
      #bossCinematic .plate{padding:18px 26px; border-radius:20px; border:1px solid rgba(255,255,255,.12); background:linear-gradient(180deg, rgba(8,14,28,.88), rgba(8,12,22,.78)); text-align:center; box-shadow:0 16px 40px rgba(0,0,0,.35);}
      #bossCinematic .eyebrow{font-size:12px; letter-spacing:.16em; text-transform:uppercase; color:#8cecff;}
      #bossCinematic .name{font-size:28px; font-weight:900; letter-spacing:.05em; margin-top:6px;}
      #bossCinematic .desc{font-size:13px; color:rgba(235,242,255,.76); margin-top:6px;}
      @media (max-width: 980px){ #metaPanel .grid{grid-template-columns:1fr;} #metaHud{right:14px; min-width:0;} }
    `;
    document.head.appendChild(style);
  }

  function buildMetaHud(){
    if(document.getElementById("metaHud")) return;
    buildMetaStyles();
    const hud = document.createElement("div");
    hud.id = "metaHud";
    hud.innerHTML = `
      <div class="title">Meta Loop / Final Build</div>
      <div class="row"><span class="soft">Loadout</span><span id="metaHudLoadout" class="accent">Vanguard</span></div>
      <div class="row"><span class="soft">Sector objective</span><span id="metaHudObjective">Scanning…</span></div>
      <div class="row"><span class="soft">Contracts</span><span id="metaHudContract">None</span></div>
      <div class="row"><span class="soft">Cores / Schematics</span><span id="metaHudRes">0 / 0</span></div>
      <div class="row"><span class="soft">Best wave</span><span id="metaHudBest">1</span></div>
      <div class="row"><span class="soft">Panel</span><span>Press <b>K</b></span></div>
    `;
    document.body.appendChild(hud);
    hud.style.display = "none";

    const wrap = document.createElement("div");
    wrap.id = "metaPanelWrap";
    wrap.innerHTML = `<div id="metaPanel"></div>`;
    document.body.appendChild(wrap);
    wrap.addEventListener("click", (e) => {
      if(e.target === wrap) toggleMetaPanel(false);
    });

    const cine = document.createElement("div");
    cine.id = "bossCinematic";
    cine.innerHTML = `<div class="plate"><div class="eyebrow">Boss Contract</div><div class="name">SIGNAL LOCK</div><div class="desc">Priority target op de arena radar.</div></div>`;
    document.body.appendChild(cine);
  }

  function metaHudEls(){
    return {
      loadout: document.getElementById("metaHudLoadout"),
      objective: document.getElementById("metaHudObjective"),
      contract: document.getElementById("metaHudContract"),
      res: document.getElementById("metaHudRes"),
      best: document.getElementById("metaHudBest")
    };
  }

  function renderMetaHud(){
    const els = metaHudEls();
    const loadout = currentLoadout();
    const objective = meta.runtime.objective;
    els.loadout.textContent = loadout.label;
    els.objective.textContent = objective ? `${objective.label} ${objective.progress}/${objective.target}` : "Awaiting directive";
    els.contract.textContent = meta.runtime.minibossAlive ? `Active · ${meta.runtime.minibossWave}` : "Idle";
    els.res.textContent = `${meta.profile.cores} / ${meta.profile.schematics}`;
    els.best.textContent = `${meta.profile.bestWave}`;
  }

  function renderMetaPanel(){
    const panel = document.getElementById("metaPanel");
    if(!panel) return;
    const loadouts = meta.profile.loadouts;
    const activeId = meta.profile.activeLoadout;
    const objective = meta.runtime.objective;
    panel.innerHTML = `
      <h2>Final Version Command Deck</h2>
      <div class="sub">Permanente progression, saveable loadouts, sector objectives en miniboss contracts.</div>
      <div class="grid">
        <div class="stack">
          <div class="card">
            <h3>Run resources</h3>
            <span class="pill">Cores <b>${meta.profile.cores}</b></span>
            <span class="pill">Schematics <b>${meta.profile.schematics}</b></span>
            <span class="pill">Best wave <b>${meta.profile.bestWave}</b></span>
            <span class="pill">Objectives <b>${meta.profile.completedObjectives}</b></span>
            <span class="pill">Contracts <b>${meta.profile.completedContracts}</b></span>
            <span class="pill">Boss contracts <b>${meta.profile.bossContracts}</b></span>
            <span class="pill">Miniboss kills <b>${meta.profile.totalMinibossKills}</b></span>
          </div>
          <div class="card">
            <h3>Loadouts</h3>
            <div class="loadouts">
              ${Object.entries(loadouts).map(([id, cfg]) => `
                <div class="loadout ${id === activeId ? "active" : ""}" data-loadout="${id}">
                  <div class="topline"><div class="name">${cfg.label}</div><div class="muted">Start: ${weaponLabelShort(cfg.startWeapon)}</div></div>
                  <div class="muted">${cfg.perk}</div>
                  <div class="muted">HP +${cfg.bonusHp} · Speed ${cfg.speed >= 0 ? "+" : ""}${cfg.speed.toFixed(2)} · Ammo ${cfg.ammo.bullet||0}/${cfg.ammo.rocket||0}/${cfg.ammo.grenade||0}</div>
                  <div class="btns"><button data-set-loadout="${id}">${id === activeId ? "Actief" : "Maak actief"}</button></div>
                </div>
              `).join("")}
            </div>
          </div>
          <div class="card">
            <h3>Live directive</h3>
            <div class="muted">Sector: ${meta.runtime.currentSector}</div>
            <div class="muted">Objective: ${objective ? `${objective.label} (${objective.progress}/${objective.target})` : "Nog geen objective actief"}</div>
            <div class="muted">Contract: ${meta.runtime.minibossAlive ? `Actief op wave ${meta.runtime.minibossWave}` : "Geen actieve miniboss contract"}</div>
            <div class="btns"><button class="alt" data-close-meta="1">Sluiten</button></div>
          </div>
        </div>
        <div class="stack">
          <div class="card">
            <h3>Weapon trees</h3>
            <div class="weapons">
              ${["bullet","rocket","grenade"].map(w => {
                const lvl = meta.profile.weaponLevel[w] || 1;
                const xp = meta.profile.weaponXP[w] || 0;
                const need = xpNeeded(lvl);
                const pct = Math.max(0, Math.min(100, (xp / need) * 100));
                const bonus = weaponTreeBonus(w);
                return `
                  <div class="weapon ${player.weapon === w ? "active" : ""}">
                    <div class="topline"><div class="name">${weaponLabelShort(w)}</div><div class="muted">Level ${lvl}</div></div>
                    <div class="muted">DMG x${bonus.damage.toFixed(2)} · Radius x${bonus.radius.toFixed(2)} · Velocity x${bonus.speed.toFixed(2)} · Heat x${bonus.heat.toFixed(2)}</div>
                    <div class="bar"><i style="width:${pct.toFixed(1)}%"></i></div>
                    <div class="muted">${xp}/${need} XP to next level</div>
                  </div>
                `;
              }).join("")}
            </div>
          </div>
          <div class="card">
            <h3>Quick controls</h3>
            <div class="muted">K = open/close panel · J = cycle loadout · 7/8/9 = direct loadout select.</div>
            <div class="muted">Objectives belonen cores en schematics. Boss contracts geven bonus progression en extra ammo.</div>
            <div class="muted">Weapon trees stijgen automatisch door kills en damage-events binnen een run.</div>
          </div>
        </div>
      </div>
    `;

    panel.querySelectorAll("[data-set-loadout]").forEach(btn => btn.addEventListener("click", () => {
      meta.profile.activeLoadout = btn.getAttribute("data-set-loadout");
      saveMetaProfile();
      renderMetaHud();
      renderMetaPanel();
      flashHint?.(`Loadout active: ${currentLoadout().label}`, 1100);
    }));
    panel.querySelectorAll("[data-close-meta]").forEach(btn => btn.addEventListener("click", () => toggleMetaPanel(false)));
  }

  function toggleMetaPanel(force){
    const wrap = document.getElementById("metaPanelWrap");
    if(!wrap) return;
    const next = typeof force === "boolean" ? force : wrap.style.display !== "flex";
    wrap.style.display = next ? "flex" : "none";
    if(next) renderMetaPanel();
  }

  function cycleLoadout(step=1){
    const ids = Object.keys(meta.profile.loadouts);
    const current = ids.indexOf(meta.profile.activeLoadout);
    const next = ids[(current + step + ids.length) % ids.length];
    meta.profile.activeLoadout = next;
    saveMetaProfile();
    renderMetaHud();
    renderMetaPanel();
    flashHint?.(`Loadout: ${meta.profile.loadouts[next].label}`, 900);
  }

  function applyLoadoutToCurrentRun(){
    const loadout = currentLoadout();
    if(!loadout || meta.runtime.loadoutApplied) return;
    meta.runtime.loadoutApplied = true;
    player.maxHp += loadout.bonusHp;
    player.hp = Math.min(player.maxHp, player.hp + loadout.bonusHp);
    player.speed += loadout.speed;
    player.ammo.bullet += loadout.ammo.bullet || 0;
    player.ammo.rocket += loadout.ammo.rocket || 0;
    player.ammo.grenade += loadout.ammo.grenade || 0;
    player.abilities.plasma += loadout.abilities.plasma || 0;
    player.abilities.mine += loadout.abilities.mine || 0;
    player.abilities.orbital += loadout.abilities.orbital || 0;
    setWeapon?.(loadout.startWeapon);
    setStat?.();
    updateHud?.();
    renderMetaHud();
  }

  function resetMetaRuntime(){
    meta.runtime.currentSector = "Core Nexus";
    meta.runtime.objective = null;
    meta.runtime.currentBossRef = null;
    meta.runtime.lastWaveSeen = 0;
    meta.runtime.minibossWave = 0;
    meta.runtime.minibossAlive = false;
    meta.runtime.runWeaponKills = { bullet:0, rocket:0, grenade:0 };
    meta.runtime.runDamageAmp = 1;
    meta.runtime.cinematicTimer = 0;
    meta.runtime.loadoutApplied = false;
  }

  function chooseObjectiveForSector(sector){
    const waveScale = Math.max(0, Math.floor(player.wave / 2));
    const templates = [
      { type:"kills", label:`Purge ${sector}`, target: 4 + waveScale },
      { type:"weapon", label:`Rifle drill · ${sector}`, weapon:"bullet", target: 3 + waveScale },
      { type:"weapon", label:`Siege break · ${sector}`, weapon:"rocket", target: 2 + Math.floor(waveScale * 0.6) },
      { type:"weapon", label:`Arc denial · ${sector}`, weapon:"grenade", target: 2 + Math.floor(waveScale * 0.6) }
    ];
    const pool = templates.filter(t => t.type !== "weapon" || t.weapon !== player.weapon || Math.random() < 0.7);
    const pick = pool[Math.floor(Math.random() * pool.length)] || templates[0];
    return Object.assign({ sector, progress:0, rewardCores: 1 + Math.floor(player.wave / 4), rewardSchematics: sector === "Core Nexus" ? 0 : 1 }, pick);
  }

  function updateObjective(forceNew=false){
    const sec = typeof sectorName === "function" ? sectorName() : "Core Nexus";
    meta.runtime.currentSector = sec;
    if(forceNew || !meta.runtime.objective || meta.runtime.objective.sector !== sec || meta.runtime.objective.completed){
      meta.runtime.objective = chooseObjectiveForSector(sec);
      flashHint?.(`Objective: ${meta.runtime.objective.label}`, 1300);
    }
    renderMetaHud();
  }

  function completeObjective(){
    const obj = meta.runtime.objective;
    if(!obj || obj.completed) return;
    obj.completed = true;
    meta.profile.cores += obj.rewardCores;
    meta.profile.schematics += obj.rewardSchematics;
    meta.profile.completedObjectives += 1;
    player.ammo.bullet += 8;
    if(player.wave >= 4) player.ammo.rocket += 1;
    if(player.wave >= 6) player.abilities.mine += 1;
    createShockwave?.(player.pos.clone(), 0x96ffd4, 4.4);
    createFlash?.(player.pos.clone().add(new THREE.Vector3(0,1.4,0)), 0x96ffd4, 2.8, 10, 0.12);
    showFloating?.(`OBJECTIVE COMPLETE +${obj.rewardCores} CORE`);
    saveMetaProfile();
    setStat?.();
    renderMetaHud();
    renderMetaPanel();
    setTimeout(() => updateObjective(true), 250);
  }

  function progressObjective(kind, weapon, amount=1){
    const obj = meta.runtime.objective;
    if(!obj || obj.completed) return;
    if(obj.type === "kills" && kind === "kill"){
      obj.progress += amount;
    } else if(obj.type === "weapon" && kind === "weapon" && obj.weapon === weapon){
      obj.progress += amount;
    }
    if(obj.progress >= obj.target) completeObjective();
    renderMetaHud();
  }

  function makeMiniboss(enemy){
    if(!enemy || enemy.isBoss || enemy.ohContractMiniBoss) return enemy;
    enemy.ohContractMiniBoss = true;
    enemy.maxHp *= 2.6;
    enemy.hp = enemy.maxHp;
    enemy.radius *= 1.18;
    enemy.speed *= 1.08;
    enemy.damageMul = (enemy.damageMul || 1) * 1.4;
    enemy.fireRateMul = Math.max(0.55, (enemy.fireRateMul || 1) * 0.74);
    enemy.contractBurstCd = 2.8;
    enemy.contractMortarCd = 4.6;
    enemy.contractLabel = `Contract ${player.wave}`;
    tintMesh?.(enemy.mesh, 0x96ffd4);
    if(enemy.groundRing?.material) enemy.groundRing.material.color.setHex(0x96ffd4);
    const label = document.getElementById("bossBarLabel");
    if(label && !state.boss) label.textContent = `${enemy.contractLabel}`;
    createShockwave?.(enemy.mesh.position.clone(), 0x96ffd4, 4.6);
    showFloating?.("MINIBOSS CONTRACT");
    return enemy;
  }

  function spawnWaveContract(){
    if(meta.runtime.minibossAlive || state.boss || !state.running || player.wave < 4) return;
    if(player.wave % 4 !== 0) return;
    const enemy = spawnEnemy?.(false);
    if(!enemy) return;
    makeMiniboss(enemy);
    meta.runtime.minibossAlive = true;
    meta.runtime.minibossWave = player.wave;
    renderMetaHud();
  }

  function showBossCinematic(title, desc){
    const cine = document.getElementById("bossCinematic");
    if(!cine) return;
    const name = cine.querySelector(".name");
    const d = cine.querySelector(".desc");
    if(name) name.textContent = title;
    if(d) d.textContent = desc;
    cine.style.display = "flex";
    meta.runtime.cinematicTimer = 1.4;
    player.damageCooldown = Math.max(player.damageCooldown || 0, 1.2);
    state.cameraShake = Math.min(2.3, (state.cameraShake || 0) + 0.8);
  }

  function updateBossCinematic(dt){
    const cine = document.getElementById("bossCinematic");
    if(!cine) return;
    if(meta.runtime.cinematicTimer > 0){
      meta.runtime.cinematicTimer -= dt;
      cine.style.opacity = Math.min(1, meta.runtime.cinematicTimer > 0.3 ? 1 : meta.runtime.cinematicTimer / 0.3);
      if(meta.runtime.cinematicTimer <= 0){
        cine.style.display = "none";
      }
    }
  }

  const _startGameFinal = startGame;
  startGame = function(){
    resetMetaRuntime();
    const res = _startGameFinal();
    applyLoadoutToCurrentRun();
    updateObjective(true);
    renderMetaHud();
    renderMetaPanel();
    return res;
  };

  const _restartGameFinal = restartGame;
  restartGame = function(){
    const res = _restartGameFinal();
    resetMetaRuntime();
    applyLoadoutToCurrentRun();
    updateObjective(true);
    renderMetaHud();
    renderMetaPanel();
    return res;
  };

  const _updateTimersFinal = updateTimers;
  updateTimers = function(dt){
    _updateTimersFinal(dt);
    if(player.wave > meta.profile.bestWave){
      meta.profile.bestWave = player.wave;
      saveMetaProfile();
    }
    if(player.wave !== meta.runtime.lastWaveSeen){
      meta.runtime.lastWaveSeen = player.wave;
      updateObjective(true);
      if(player.wave >= 4){
        setTimeout(() => {
          if(state.running && player.alive && player.wave === meta.runtime.lastWaveSeen) spawnWaveContract();
        }, 900);
      }
    } else {
      const sec = typeof sectorName === "function" ? sectorName() : "Core Nexus";
      if(sec !== meta.runtime.currentSector) updateObjective(true);
    }
    updateBossCinematic(dt);
    renderMetaHud();
  };

  const _shootWithDirectionFinal = shootWithDirection;
  shootWithDirection = function(dirOverride=null){
    const weaponBefore = player.weapon;
    const before = state.bullets.length;
    const ok = _shootWithDirectionFinal(dirOverride);
    if(ok){
      const bonus = weaponTreeBonus(weaponBefore);
      for(let i=before; i<state.bullets.length; i++){
        const b = state.bullets[i];
        if(!b) continue;
        if(b.v2Friendly !== false){
          b.damage *= bonus.damage;
          if(b.radius) b.radius *= bonus.radius;
          if(b.vel?.multiplyScalar) b.vel.multiplyScalar(bonus.speed);
          b.ohFinalWeapon = weaponBefore;
        }
      }
    }
    return ok;
  };

  const _updateHazardsFinal = updateHazards;
  updateHazards = function(dt){
    _updateHazardsFinal(dt);
    for(const enemy of state.enemies){
      if(enemy?.ohContractMiniBoss){
        enemy.contractBurstCd -= dt;
        enemy.contractMortarCd -= dt;
        if(enemy.contractBurstCd <= 0){
          enemy.contractBurstCd = 2.1;
          createShockwave?.(enemy.mesh.position.clone(), 0x96ffd4, 3.3);
          for(let i=0;i<3;i++){
            setTimeout(() => {
              if(state.running && enemy.hp > 0) enemyShoot?.(enemy);
            }, i * 80);
          }
        }
        if(enemy.contractMortarCd <= 0){
          enemy.contractMortarCd = 4.2;
          spawnBossMortarField?.(player.pos.clone(), 1, 3.0, 0x96ffd4);
        }
      }
    }
  };

  const _killEnemyFinal = killEnemy;
  killEnemy = function(enemy){
    const weaponUsed = player.weapon;
    if(enemy){
      progressObjective("kill", weaponUsed, 1);
      progressObjective("weapon", weaponUsed, 1);
      awardWeaponXP(weaponUsed, enemy.isBoss ? 24 : enemy.ohContractMiniBoss ? 18 : enemy.type === "elite" ? 12 : 8);
      meta.runtime.runWeaponKills[weaponUsed] = (meta.runtime.runWeaponKills[weaponUsed] || 0) + 1;
    }
    if(enemy?.ohContractMiniBoss){
      meta.runtime.minibossAlive = false;
      meta.profile.completedContracts += 1;
      meta.profile.totalMinibossKills += 1;
      meta.profile.cores += 3;
      meta.profile.schematics += 2;
      player.ammo.rocket += 2;
      player.ammo.grenade += 1;
      player.abilities.orbital += 1;
      saveMetaProfile();
      flashHint?.("Contract cleared · bonus rewards issued", 1300);
      showFloating?.("CONTRACT CLEARED");
      renderMetaHud();
      renderMetaPanel();
    }
    if(enemy?.isBoss){
      meta.profile.bossContracts += 1;
      meta.profile.cores += 2;
      meta.profile.schematics += 2;
      saveMetaProfile();
    }
    return _killEnemyFinal(enemy);
  };

  const _spawnEnemyFinal = spawnEnemy;
  spawnEnemy = function(isBoss=false){
    const enemy = _spawnEnemyFinal(isBoss);
    if(isBoss && enemy){
      meta.runtime.currentBossRef = enemy;
      const title = enemy.bossLabel || "Priority Target";
      const desc = enemy.v2BossProfile?.desc || enemy.profile?.directive || "Sector elimination protocol active.";
      showBossCinematic(title, desc);
    }
    return enemy;
  };

  const _applyDamageFinal = applyDamage;
  applyDamage = function(amount){
    const reduced = amount * (currentLoadout().label === "Vanguard" ? 0.96 : 1);
    return _applyDamageFinal(reduced);
  };

  window.addEventListener("keydown", (e) => {
    if(e.code === "KeyK" && !e.repeat){
      e.preventDefault();
      toggleMetaPanel();
    }
    if(e.code === "KeyJ" && !e.repeat){
      e.preventDefault();
      cycleLoadout(1);
    }
    if(e.code === "Digit7" && !e.repeat){ meta.profile.activeLoadout = "vanguard"; saveMetaProfile(); renderMetaHud(); renderMetaPanel(); }
    if(e.code === "Digit8" && !e.repeat){ meta.profile.activeLoadout = "demolisher"; saveMetaProfile(); renderMetaHud(); renderMetaPanel(); }
    if(e.code === "Digit9" && !e.repeat){ meta.profile.activeLoadout = "tactician"; saveMetaProfile(); renderMetaHud(); renderMetaPanel(); }
  }, { passive:false });

  buildMetaHud();
  renderMetaHud();
  renderMetaPanel();
})();


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

# =============================
# ONLINE LEADERBOARD API
# =============================

LEADERBOARD_WINDOW_DAYS = 30
LEADERBOARD_RATE_LIMIT_SECONDS = 20
LEADERBOARD_MAX_LIMIT = 50
PLAYER_NAME_RE = re.compile(r"[^A-Za-z0-9 _\-\.]+")


def leaderboard_cutoff_iso(days=LEADERBOARD_WINDOW_DAYS):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def normalize_player_name(value):
    value = (value or "Speler").strip()[:18]
    value = PLAYER_NAME_RE.sub("", value).strip()
    return value or "Speler"


def safe_int(value, default=0, minimum=None, maximum=None):
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:64]
    return (request.remote_addr or "")[:64]


def cleanup_old_scores():
    conn = db()
    try:
        conn.execute(
            "DELETE FROM leaderboard_scores WHERE created_at < ?",
            (leaderboard_cutoff_iso(LEADERBOARD_WINDOW_DAYS),)
        )
        conn.commit()
    finally:
        conn.close()


def leaderboard_recent_submit_exists(conn, ip):
    if not ip:
        return False
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=LEADERBOARD_RATE_LIMIT_SECONDS)).isoformat()
    row = conn.execute(
        """
        SELECT 1
        FROM leaderboard_scores
        WHERE ip = ? AND created_at >= ?
        LIMIT 1
        """,
        (ip, recent_cutoff)
    ).fetchone()
    return row is not None


@app.route("/api/leaderboard/submit", methods=["POST"])
def leaderboard_submit():
    data = request.get_json(silent=True) or {}

    name = normalize_player_name(data.get("name"))
    score = safe_int(data.get("score"), default=0, minimum=0, maximum=10_000_000)
    wave = safe_int(data.get("wave"), default=0, minimum=0, maximum=10_000)

    if score <= 0:
        return jsonify({"ok": False, "error": "invalid_score"}), 400

    ip = client_ip()
    user_agent = (request.headers.get("User-Agent", "") or "")[:200]
    created_at = datetime.now(timezone.utc).isoformat()

    conn = db()
    try:
        if leaderboard_recent_submit_exists(conn, ip):
            return jsonify({"ok": False, "error": "rate_limited"}), 429

        conn.execute(
            """
            INSERT INTO leaderboard_scores
            (player_name, score, wave, created_at, ip, user_agent)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (name, score, wave, created_at, ip, user_agent)
        )
        conn.commit()
    finally:
        conn.close()

    cleanup_old_scores()
    return jsonify({"ok": True})


@app.route("/api/leaderboard/top")
def leaderboard_top():
    limit = safe_int(request.args.get("limit", 10), default=10, minimum=1, maximum=LEADERBOARD_MAX_LIMIT)
    cutoff = leaderboard_cutoff_iso(LEADERBOARD_WINDOW_DAYS)

    conn = db()
    try:
        rows = conn.execute(
            """
            SELECT player_name, score, wave
            FROM leaderboard_scores
            WHERE created_at >= ?
            ORDER BY score DESC, wave DESC, created_at ASC
            LIMIT ?
            """,
            (cutoff, limit)
        ).fetchall()
    finally:
        conn.close()

    return jsonify({
        "rows": [
            {
                "name": r["player_name"],
                "score": r["score"],
                "wave": r["wave"]
            }
            for r in rows
        ]
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
