#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========= MiniTransfer – Olde Hanter (met abonnementbeheer + PayPal webhook) =========
# - Upload (files/folders) naar B2 (S3) met voortgang
# - Downloadpagina met zip-stream en precheck
# - Contact/aanvraag met PayPal abonnement-knop (pas zichtbaar bij volledig geldig formulier)
# - Abonnementbeheer: opslaan subscriptionID, opzeggen, plan wijzigen (revise)
# - Webhook: verifieert PayPal-events en mailt bij activatie/annulering/suspense/reactivatie en bij elke capture
# - Domeinen: ondersteunt minitransfer.onrender.com én downloadlink.nl in get_base_host()
# ======================================================================================

import os, re, uuid, smtplib, sqlite3, logging, base64, json, urllib.request, hmac, time, secrets, threading
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

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

AUTH_EMAIL = os.environ.get("AUTH_EMAIL", "").strip().lower()
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", "").strip()
# Backward compat: accepteer ook plaintext env var, maar waarschuw. Nieuwe installs horen AUTH_PASSWORD_HASH te gebruiken.
_AUTH_PASSWORD_PLAIN = os.environ.get("AUTH_PASSWORD", "").strip()

if not AUTH_EMAIL:
    raise RuntimeError(
        "❌ AUTH_EMAIL ontbreekt! Zet AUTH_EMAIL in Render → Environment."
    )
if not AUTH_PASSWORD_HASH and not _AUTH_PASSWORD_PLAIN:
    raise RuntimeError(
        "❌ Auth-configuratie mist! Zet AUTH_PASSWORD_HASH (aanbevolen) of AUTH_PASSWORD in Render → Environment."
    )

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
MAIL_TO   = os.environ.get("MAIL_TO", "").strip()

# Extra wachtwoord voor het bekijken van pakketten van *andere* gebruikers
# binnen hetzelfde (gedeelde) account. Voorkomt dat collega A via de
# 'Mijn uploads'-lijst de download-pagina van collega B opent en bestanden
# downloadt die niet voor hem bedoeld zijn.
#
# Werking:
# - Bezoekers die NIET ingelogd zijn (externe ontvangers met share-link):
#   geen extra prompt. Normaal pad.
# - Ingelogde eigenaar of admin: geen extra prompt voor eigen pakketten.
# - Ingelogde gebruiker die een pakket van een ander opent: moet eerst
#   dit wachtwoord invoeren. Wordt per sessie onthouden (TTL).
# Leeg laten = feature uit (bestaand gedrag).
MY_UPLOADS_PASSWORD = os.environ.get("MY_UPLOADS_PASSWORD", "").strip()
MY_UPLOADS_UNLOCK_TTL = int(os.environ.get("MY_UPLOADS_UNLOCK_TTL", "1800"))

# PayPal Subscriptions
PAYPAL_CLIENT_ID     = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE      = os.environ.get("PAYPAL_API_BASE", "https://api-m.paypal.com")  # sandbox: https://api-m.sandbox.paypal.com

PAYPAL_PLAN_0_5  = os.environ.get("PAYPAL_PLAN_0_5", "").strip()  # 0,5 TB – €12/mnd
PAYPAL_PLAN_1    = os.environ.get("PAYPAL_PLAN_1",   "").strip()  # 1 TB   – €15/mnd
PAYPAL_PLAN_2    = os.environ.get("PAYPAL_PLAN_2",   "").strip()  # 2 TB   – €20/mnd
PAYPAL_PLAN_5    = os.environ.get("PAYPAL_PLAN_5",   "").strip()  # 5 TB   – €30/mnd

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
_secret = os.environ.get("SECRET_KEY")
if not _secret:
    raise RuntimeError(
        "❌ SECRET_KEY ontbreekt! Zet SECRET_KEY in Render → Environment. "
        "Tip: render.yaml kan met generateValue: true een willekeurige waarde aanmaken."
    )
app.config["SECRET_KEY"] = _secret
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_CONTENT_LENGTH", str(1024 * 1024 * 1024 * 20))),
)

# 16 hex tekens (64 bits) voor nieuwe tokens. Oude 10-hex tokens blijven geaccepteerd
# voor terugwaartse compatibiliteit met reeds gedeelde links.
TOKEN_RE = re.compile(r"^[a-f0-9]{10}$|^[a-f0-9]{16}$")
NEW_TOKEN_BYTES = 8  # 8 bytes = 16 hex = 64 bits
MIN_EXPIRY_DAYS = float(os.environ.get("MIN_EXPIRY_DAYS", "0.04"))  # ~1 uur
MAX_EXPIRY_DAYS = float(os.environ.get("MAX_EXPIRY_DAYS", "365"))
MAX_TITLE_LENGTH = int(os.environ.get("MAX_TITLE_LENGTH", "120"))

# Sentinel waarde voor onbeperkt geldige pakketten.
# We gebruiken een vaste datum ver in de toekomst (jaar 9999) zodat alle bestaande
# expiry-checks (pkg["expires_at"] <= now) en de cleanup_expired-routine ongewijzigd
# blijven werken: het pakket verloopt simpelweg nooit.
NEVER_EXPIRES_AT = datetime(9999, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
NEVER_EXPIRES_ISO = NEVER_EXPIRES_AT.isoformat()

# --- Render healthcheck fix ---
HEALTH_PATHS = ("/health", "/health-s3", "/__health")

@app.before_request
def allow_health():
    """Laat Render healthchecks (en vergelijkbare) gewoon 200 OK teruggeven."""
    if request.path.startswith(HEALTH_PATHS):
        return  # Geen redirect of blokkade; laat de route doorgaan

# ---- Multi-tenant configuratie (HOST -> tenant) ----
# Je kunt TENANT_HOST + TENANT_SLUG expliciet zetten, maar als ze ontbreken
# wordt er afgeleid uit CANONICAL_HOST. Bijvoorbeeld 'oldehanter.downloadlink.nl'
# => host=oldehanter.downloadlink.nl, slug=oldehanter.
_tenant_host = os.environ.get("TENANT_HOST", "").strip().lower()
_tenant_slug = os.environ.get("TENANT_SLUG", "").strip().lower()

if not _tenant_host:
    _tenant_host = os.environ.get("CANONICAL_HOST", "").strip().lower()
if not _tenant_slug and _tenant_host:
    _tenant_slug = _tenant_host.split(".", 1)[0]

if not _tenant_host or not _tenant_slug:
    raise RuntimeError(
        "❌ Tenant-configuratie mist! Zet CANONICAL_HOST (of TENANT_HOST + TENANT_SLUG) in Render → Environment."
    )

TENANTS = {
    _tenant_host: {
        "slug": _tenant_slug,
        "mail_to": MAIL_TO,
        "is_trial": False,
    }
}
_DEFAULT_TENANT_HOST = _tenant_host

# ---- Branding ----
# Branding wordt automatisch afgeleid uit gegevens die er al zijn:
#   * Voor klant-tenants: het 'Bedrijf'-veld uit het contactformulier
#     (opgeslagen in pending_accounts.company en/of de tenant_brand-tabel).
#   * Voor de hoofd-tenant (de eigenaar van de installatie zelf): een
#     fallback-naam, omdat die geen klant-aanvraag heeft. Standaard 'Olde
#     Hanter Bouwconstructies' om bestaande deploys ongewijzigd te laten;
#     overrid'baar via één env var.
#   * Voor de trial-tenant: vaste neutrale branding ('Trial').
#   * Voor users-rows zonder bekende bedrijfsnaam: nette title-case van de slug.
# Initialen voor de logo-badge worden dynamisch berekend uit de bedrijfsnaam.
_HOST_BRAND_NAME = os.environ.get("HOST_BRAND_NAME", "Olde Hanter Bouwconstructies").strip()

# ---- Trial-tenant (gratis variant met restricties) ----
# Trial draait standaard op het canonieke domein onder pad /trial/...
# (bv. https://downloadlink.nl/trial/signup). Optioneel kan via TRIAL_HOST
# een apart subdomein worden geconfigureerd; dan wordt dat subdomein óók
# als trial-host herkend (achterwaarts compatibel met oudere setups).
_trial_host = os.environ.get("TRIAL_HOST", "").strip().lower()  # optioneel, leeg = geen subdomein
TRIAL_TENANT_SLUG = "trial"
TRIAL_ENABLED = os.environ.get("TRIAL_ENABLED", "1").lower() in ("1", "true", "yes")
# Interne sleutel voor de trial-tenant in TENANTS. Begint met '__' zodat hij
# nooit per ongeluk matcht op een echte Host-header.
_TRIAL_TENANT_KEY = "__trial__"
if TRIAL_ENABLED:
    TENANTS[_TRIAL_TENANT_KEY] = {
        "slug": TRIAL_TENANT_SLUG,
        "mail_to": MAIL_TO,
        "is_trial": True,
    }
    # Backwards compatibility: als TRIAL_HOST expliciet is gezet, blijft de
    # oude subdomein-routing óók werken.
    if _trial_host:
        TENANTS[_trial_host] = TENANTS[_TRIAL_TENANT_KEY]

# Trial-restricties (overrid'baar via env vars zonder code-aanpassing)
TRIAL_MAX_BYTES_PER_PACKAGE = int(os.environ.get("TRIAL_MAX_BYTES_PER_PACKAGE", str(2 * 1024 * 1024 * 1024)))  # 2 GB
TRIAL_MAX_TTL_DAYS          = float(os.environ.get("TRIAL_MAX_TTL_DAYS", "3"))
TRIAL_MAX_ACTIVE_PACKAGES   = int(os.environ.get("TRIAL_MAX_ACTIVE_PACKAGES", "5"))
TRIAL_VERIFY_TOKEN_TTL_HRS  = int(os.environ.get("TRIAL_VERIFY_TOKEN_TTL_HRS", "48"))

def current_tenant():
    # 1) Ingelogde gebruiker: zijn/haar eigen tenant is leidend. Een trial-user
    #    moet trial-tenant houden ongeacht via welk pad hij iets doet (uploaden,
    #    /uploads bekijken, etc.) — anders raken pakketten zoek doordat ze aan
    #    een andere tenant gekoppeld worden dan de gebruiker zelf.
    try:
        # Dit is gecached via 'g' per request, dus geen extra DB-hit.
        u = current_user()
    except RuntimeError:
        # Buiten request-context (bv. tijdens app-startup): val door.
        u = None
    if u:
        slug = u["tenant_id"]
        for tenant in TENANTS.values():
            if tenant.get("slug") == slug:
                return tenant
        # Slug onbekend in TENANTS → val door naar pad/host-detectie.

    # 2) Anonieme bezoeker: paden onder /trial/ horen bij de trial-tenant.
    if TRIAL_ENABLED:
        try:
            path = (request.path or "")
        except RuntimeError:
            path = ""
        if path.startswith("/trial/") or path == "/trial":
            return TENANTS[_TRIAL_TENANT_KEY]

    # 3) Fallback: host-matching, met canonical default.
    host = (request.headers.get("Host") or "").lower()
    return TENANTS.get(host) or TENANTS[_DEFAULT_TENANT_HOST]

def is_trial_tenant() -> bool:
    return bool(current_tenant().get("is_trial"))

def _compute_initials(name: str) -> str:
    """Bereken een 1-2 letter initialen-badge uit een bedrijfsnaam.

    Voorbeelden:
      'Vericon'                          -> 'V'
      'Rensing Bouw'                     -> 'RB'
      'Olde Hanter Bouwconstructies'     -> 'OH' (max 2)
      'Bouwbedrijf B.V.'                 -> 'B'   (B.V. wordt eraf gestript)
      'Bouwbedrijf de Vries'             -> 'BV'  ('de' wordt geskipt)
      ''                                 -> 'FT'  (fallback)
    """
    if not name:
        return "FT"
    import re as _re
    # Strip rechtsvorm-suffixen vóór het splitten — anders worden ze als losse
    # letters meegeteld (bv. 'B.V.' zou 'B' en 'V' opleveren).
    cleaned_name = _re.sub(
        r"\b(b\.?v\.?|n\.?v\.?|v\.?o\.?f\.?|c\.?v\.?|gmbh|ltd|llc|inc)\b\.?",
        "",
        name,
        flags=_re.IGNORECASE,
    )
    # Filter veelgebruikte stopwoorden die niets aan de identiteit toevoegen.
    _SKIP = {"the", "de", "het", "van", "der", "den"}
    words = [w for w in _re.split(r"[^A-Za-zÀ-ÿ]+", cleaned_name) if w]
    words = [w for w in words if w.lower() not in _SKIP]
    if not words:
        # Niets bruikbaars overgebleven; pak gewoon eerste 2 letters van de input.
        only_letters = _re.sub(r"[^A-Za-zÀ-ÿ]", "", name)
        return (only_letters[:2] or "FT").upper()
    if len(words) == 1:
        # Eén woord: alleen de eerste letter.
        return words[0][0].upper()
    # Meerdere woorden: eerste letter van de eerste 2 betekenisvolle woorden.
    # Maximaal 2 letters, omdat 3 letters in een favicon slecht leesbaar is.
    return "".join(w[0] for w in words[:2]).upper()


def _slug_to_pretty(slug: str) -> str:
    """Maak een nette weergave-naam uit een tenant-slug. Laatste vangnet als
    er geen company-naam in de DB staat. 'rensingbouw' -> 'Rensingbouw'."""
    if not slug:
        return "Bestandentransfer"
    return slug.replace("-", " ").replace("_", " ").strip().title()


def _lookup_company_for_tenant(slug: str) -> str:
    """Haal de bedrijfsnaam op voor een tenant_id uit de tenant_brand-cache,
    met fallback op de meest recente pending_accounts.company van die tenant.
    Geeft '' terug als er niets bekend is.
    """
    if not slug:
        return ""
    try:
        conn = db()
        try:
            row = conn.execute(
                "SELECT company_name FROM tenant_brand WHERE tenant_id = ?",
                (slug,)
            ).fetchone()
            if row and row["company_name"]:
                return row["company_name"]
            # Fallback: pak de laatste pending met een ingevulde company.
            row = conn.execute(
                "SELECT company FROM pending_accounts "
                "WHERE tenant_id = ? AND company IS NOT NULL AND company != '' "
                "ORDER BY created_at DESC LIMIT 1",
                (slug,)
            ).fetchone()
            if row and row["company"]:
                return row["company"]
        finally:
            conn.close()
    except Exception:
        log.exception("brand lookup failed for tenant=%s", slug)
    return ""


def current_brand() -> dict:
    """Geeft de branding voor de huidige tenant — automatisch afgeleid.

    Bron-volgorde:
      1. Trial-tenant -> vaste neutrale 'Trial'-branding.
      2. Hoofd-tenant (de eigenaar van de installatie zelf) -> HOST_BRAND_NAME
         env var, default 'Olde Hanter Bouwconstructies'.
      3. Klant-tenants -> company_name uit tenant_brand-tabel, met fallback op
         meest recente pending_accounts.company, met fallback op title-case
         van de slug.
    Initialen voor de logo-badge worden automatisch berekend uit de naam.
    Gecached in flask.g per request zodat herhaalde calls geen DB-hits doen.
    """
    # Per-request cache (g) zodat dezelfde response niet 5 DB-hits doet.
    try:
        cached = getattr(g, "_brand_cache", None)
        if cached is not None:
            return cached
    except RuntimeError:
        # Buiten request-context: geen caching mogelijk, val door.
        cached = None

    t = current_tenant()
    if t.get("is_trial"):
        name = "Trial · Bestandentransfer"
        tagline = "Beveiligde bestandsoverdracht (trial)"
        footer = "Trial · Bestandentransfer"
    elif t.get("slug") == _tenant_slug:
        # Hoofd-tenant (eigenaar van de installatie). Geen pending-aanvraag, dus
        # branding komt uit env var met sensible default.
        name = _HOST_BRAND_NAME
        tagline = "Beveiligde bestandsoverdracht"
        footer = f"{name} · Bestandentransfer"
    else:
        # Klant-tenant: zoek de bedrijfsnaam op in de DB.
        company = _lookup_company_for_tenant(t.get("slug", ""))
        name = company or _slug_to_pretty(t.get("slug", ""))
        tagline = "Beveiligde bestandsoverdracht"
        footer = f"{name} · Bestandentransfer"

    brand = {
        "name":     name,
        "short":    _compute_initials(name),
        "tagline":  tagline,
        "footer":   footer,
        "logo_svg": "",   # geen per-tenant logo (zou handmatig moeten)
        "color":    "",   # geen per-tenant kleur
    }
    try:
        g._brand_cache = brand
    except RuntimeError:
        pass
    return brand

def trial_signup_url() -> str:
    """Geeft de absolute URL naar de trial-signup-pagina, of '' als trial uit staat.

    Standaard: https://<canonical_host>/trial/signup (op de huidige server).
    Als TRIAL_HOST is gezet, wordt dat subdomein gebruikt (legacy).
    """
    if not TRIAL_ENABLED:
        return ""
    host = _trial_host or _tenant_host
    return f"https://{host}/trial/signup"
# ----------------------------------------------------

# --- Redirect config toevoegen ---
from werkzeug.middleware.proxy_fix import ProxyFix

app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_port=1)
app.config.update(PREFERRED_URL_SCHEME="https", SESSION_COOKIE_SECURE=True)

CANONICAL_HOST = os.environ.get("CANONICAL_HOST", _tenant_host).lower()
OLD_HOST = os.environ.get("OLD_HOST", "").strip().lower()

@app.before_request
def _redirect_old_host():
    if not OLD_HOST:
        return
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
      CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        tenant_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id)")

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

    # ===== PENDING ACCOUNTS =====
    # Aanvragen die nog niet betaald zijn. Na succesvolle PayPal-activatie wordt
    # deze rij gebruikt om een echte users-row aan te maken.
    c.execute("""
      CREATE TABLE IF NOT EXISTS pending_accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        plan_value TEXT,
        company TEXT,
        phone TEXT,
        notes TEXT,
        paypal_subscription_id TEXT UNIQUE,
        status TEXT NOT NULL DEFAULT 'awaiting_payment',
        created_at TEXT NOT NULL,
        activated_at TEXT
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_pending_email_tenant ON pending_accounts(email, tenant_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pending_sub ON pending_accounts(paypal_subscription_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_accounts(status)")

    # ===== TENANT BRAND =====
    # Cache van bedrijfsnaam per tenant_id, zodat de UI per ingelogde gebruiker
    # automatisch de juiste branding kan tonen zonder env-config per tenant.
    # Wordt gevuld vanuit pending_accounts.company op het moment dat een
    # PayPal-aanvraag wordt geactiveerd. Eén rij per tenant_id.
    c.execute("""
      CREATE TABLE IF NOT EXISTS tenant_brand (
        tenant_id TEXT PRIMARY KEY,
        company_name TEXT NOT NULL,
        updated_at TEXT NOT NULL
      )
    """)

    # ===== RATE LIMITING (login + package views) =====
    c.execute("""
      CREATE TABLE IF NOT EXISTS rate_limits (
        scope TEXT NOT NULL,
        ip TEXT NOT NULL,
        count INTEGER NOT NULL DEFAULT 0,
        first_ts REAL NOT NULL,
        blocked_until REAL NOT NULL DEFAULT 0,
        PRIMARY KEY (scope, ip)
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_rate_limits_first_ts ON rate_limits(first_ts)")

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

    # ===== DOWNLOAD ANALYTICS =====
    c.execute("""
      CREATE TABLE IF NOT EXISTS download_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL,
        item_id INTEGER,
        download_type TEXT NOT NULL,   -- 'file' of 'zip'
        downloaded_at TEXT NOT NULL,
        ip TEXT,
        user_agent TEXT,
        tenant_id TEXT NOT NULL
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_download_events_token ON download_events(token)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_download_events_tenant ON download_events(tenant_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_download_events_downloaded_at ON download_events(downloaded_at)")

    # ===== EMAIL VERIFICATIONS =====
    # Gebruikt voor de gratis trial-signup. Eén rij per uitstaande verificatie;
    # consumed_at wordt gezet zodra de gebruiker de link aanklikt. Tokens zijn
    # 32 hex chars (128 bits) zodat ze niet te raden zijn.
    c.execute("""
      CREATE TABLE IF NOT EXISTS email_verifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT UNIQUE NOT NULL,
        email TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        tenant_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        ip TEXT
      )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_token ON email_verifications(token)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_email ON email_verifications(email)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_expires ON email_verifications(expires_at)")

    c.commit()
    c.close()

init_db()

def _col_exists(conn, table, col):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(r[1] == col for r in cur.fetchall())

def migrate_add_tenant_columns():
    conn = db()
    try:
        default_slug = _tenant_slug
        # packages
        if not _col_exists(conn, "packages", "tenant_id"):
            conn.execute("ALTER TABLE packages ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE packages SET tenant_id = ? WHERE tenant_id IS NULL", (default_slug,))
        # items
        if not _col_exists(conn, "items", "tenant_id"):
            conn.execute("ALTER TABLE items ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE items SET tenant_id = ? WHERE tenant_id IS NULL", (default_slug,))
        # subscriptions
        if not _col_exists(conn, "subscriptions", "tenant_id"):
            conn.execute("ALTER TABLE subscriptions ADD COLUMN tenant_id TEXT")
            conn.execute("UPDATE subscriptions SET tenant_id = ? WHERE tenant_id IS NULL", (default_slug,))

        conn.commit()
    finally:
        conn.close()

migrate_add_tenant_columns()

def migrate_add_owner_columns():
    """Voeg owner_user_id toe aan packages (per-user scoping)."""
    conn = db()
    try:
        if not _col_exists(conn, "packages", "owner_user_id"):
            conn.execute("ALTER TABLE packages ADD COLUMN owner_user_id INTEGER")
        conn.commit()
    finally:
        conn.close()

migrate_add_owner_columns()


def migrate_add_download_analytics():
    conn = db()
    try:
        conn.execute("""
          CREATE TABLE IF NOT EXISTS download_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL,
            item_id INTEGER,
            download_type TEXT NOT NULL,
            downloaded_at TEXT NOT NULL,
            ip TEXT,
            user_agent TEXT,
            tenant_id TEXT NOT NULL
          )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_events_token ON download_events(token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_events_tenant ON download_events(tenant_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_download_events_downloaded_at ON download_events(downloaded_at)")
        conn.commit()
    finally:
        conn.close()

migrate_add_download_analytics()

def migrate_add_trial_columns():
    """Voeg is_trial + email_verified toe aan users, en de email_verifications tabel.

    Idempotent: doet niets als kolommen/tabel al bestaan. Veilig om bij elke
    deploy te draaien.
    """
    conn = db()
    try:
        if not _col_exists(conn, "users", "is_trial"):
            conn.execute("ALTER TABLE users ADD COLUMN is_trial INTEGER NOT NULL DEFAULT 0")
        if not _col_exists(conn, "users", "email_verified"):
            conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 1")
        conn.execute("""
          CREATE TABLE IF NOT EXISTS email_verifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT UNIQUE NOT NULL,
            email TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            tenant_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            consumed_at TEXT,
            ip TEXT
          )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_token ON email_verifications(token)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_email ON email_verifications(email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_email_verif_expires ON email_verifications(expires_at)")
        conn.commit()
    finally:
        conn.close()

migrate_add_trial_columns()

def migrate_align_package_tenants_with_owner():
    """
    Eénmalige fix: oudere trial-pakketten kunnen tenant_id='downloadlink'
    (of 'oldehanter') hebben terwijl hun eigenaar tenant_id='trial' is.
    Dat ontstond doordat current_tenant() destijds op het URL-pad keek;
    een trial-user die op /upload uploadde kreeg de canonical tenant.

    Deze migratie zet packages en items op de tenant van hun eigenaar.
    Idempotent: opnieuw draaien doet niks als alles al klopt.
    """
    conn = db()
    try:
        # Aantal mismatches tellen voor logging.
        n_pkgs = conn.execute("""
            SELECT COUNT(*) AS n FROM packages p
            JOIN users u ON u.id = p.owner_user_id
            WHERE p.owner_user_id IS NOT NULL
              AND p.tenant_id != u.tenant_id
        """).fetchone()["n"]
        if not n_pkgs:
            return

        log.info("Tenant-migratie: %d package(s) met afwijkende tenant t.o.v. eigenaar — corrigeren", n_pkgs)

        # Items volgen het pakket: zet items op dezelfde nieuwe tenant
        # (gekoppeld via token + huidige pakket-tenant).
        conn.execute("""
            UPDATE items
               SET tenant_id = (
                   SELECT u.tenant_id
                     FROM packages p
                     JOIN users u ON u.id = p.owner_user_id
                    WHERE p.token = items.token
                      AND p.tenant_id = items.tenant_id
                      AND p.owner_user_id IS NOT NULL
                      AND p.tenant_id != u.tenant_id
               )
             WHERE EXISTS (
                   SELECT 1 FROM packages p
                     JOIN users u ON u.id = p.owner_user_id
                    WHERE p.token = items.token
                      AND p.tenant_id = items.tenant_id
                      AND p.owner_user_id IS NOT NULL
                      AND p.tenant_id != u.tenant_id
               )
        """)

        # Pakket zelf op tenant van eigenaar zetten.
        conn.execute("""
            UPDATE packages
               SET tenant_id = (
                   SELECT u.tenant_id FROM users u WHERE u.id = packages.owner_user_id
               )
             WHERE owner_user_id IS NOT NULL
               AND tenant_id != (
                   SELECT u.tenant_id FROM users u WHERE u.id = packages.owner_user_id
               )
        """)

        conn.commit()
        log.info("Tenant-migratie klaar.")
    except Exception:
        log.exception("migrate_align_package_tenants_with_owner faalde")
    finally:
        conn.close()

migrate_align_package_tenants_with_owner()

def seed_admin_from_env():
    """
    Bij eerste start (of wanneer de admin nog niet bestaat): maak admin-user aan
    op basis van AUTH_EMAIL + AUTH_PASSWORD_HASH (of AUTH_PASSWORD als legacy).
    Koppel bestaande packages zonder eigenaar aan deze admin.
    """
    if not AUTH_EMAIL:
        return
    # Bepaal wachtwoord-hash
    pw_hash = AUTH_PASSWORD_HASH
    if not pw_hash and _AUTH_PASSWORD_PLAIN:
        pw_hash = generate_password_hash(_AUTH_PASSWORD_PLAIN)
    if not pw_hash:
        return

    conn = db()
    try:
        existing = conn.execute("SELECT id, is_admin FROM users WHERE email = ?", (AUTH_EMAIL,)).fetchone()
        now_iso = datetime.now(timezone.utc).isoformat()
        if existing is None:
            conn.execute(
                "INSERT INTO users(email, password_hash, is_admin, tenant_id, created_at, disabled) VALUES(?,?,?,?,?,0)",
                (AUTH_EMAIL, pw_hash, 1, _tenant_slug, now_iso)
            )
            admin_id = conn.execute("SELECT id FROM users WHERE email = ?", (AUTH_EMAIL,)).fetchone()["id"]
            log.info("Seeded admin user %s (id=%s)", AUTH_EMAIL, admin_id)
        else:
            admin_id = existing["id"]
            # Zorg dat admin-flag staat
            if not existing["is_admin"]:
                conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (admin_id,))
        # Koppel losgekoppelde packages aan admin
        conn.execute(
            "UPDATE packages SET owner_user_id = ? WHERE owner_user_id IS NULL AND tenant_id = ?",
            (admin_id, _tenant_slug)
        )
        conn.commit()
    finally:
        conn.close()

seed_admin_from_env()

def _insert_download_event(token, item_id, download_type, downloaded_at, ip, user_agent, tenant_id):
    """Daadwerkelijke INSERT — wordt op een achtergrondthread gerund."""
    try:
        conn = db()
        try:
            conn.execute("""
                INSERT INTO download_events (
                    token, item_id, download_type, downloaded_at, ip, user_agent, tenant_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (token, item_id, download_type, downloaded_at, ip, user_agent, tenant_id))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("download event insert failed")

def log_download_event(token: str, tenant_id: str, download_type: str, item_id=None):
    # Capture request-context waarden NU (anders zijn ze weg op de bg-thread).
    payload = (
        token,
        item_id,
        download_type,
        datetime.now(timezone.utc).isoformat(),
        client_ip(),
        (request.headers.get("User-Agent") or "")[:500],
        tenant_id,
    )
    # Fire-and-forget: blokkeert de download-request niet.
    # _bg_executor wordt verderop gedefinieerd; lazy lookup via globals().
    try:
        ex = globals().get("_bg_executor")
        if ex is not None:
            ex.submit(_insert_download_event, *payload)
            return
    except Exception:
        log.exception("download event submit failed")
    # Fallback (executor nog niet beschikbaar bij init): synchroon.
    _insert_download_event(*payload)

def parse_dt_utc(iso_value, default=None):
    """Parse een ISO-datetime en garandeer timezone-aware UTC.

    Beschermt tegen 'naive vs aware' TypeError-crashes als er ooit een rij
    zonder timezone-info in de DB belandt. Accepteert ook 'Z'-suffix.
    """
    if not iso_value:
        return default
    try:
        s = iso_value
        if isinstance(s, str) and s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s) if isinstance(s, str) else s
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        return dt
    except Exception:
        return default

def format_nl_datetime(iso_value):
    if not iso_value:
        return "—"
    dt = parse_dt_utc(iso_value)
    if dt is None:
        return iso_value
    return dt.strftime("%d-%m-%Y %H:%M UTC")

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
.footer{color:var(--muted);margin-top:1.2rem;text-align:center}
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

/* ======= Verfijnde knop-variant (stijl uit header, voor lichte kaarten) ======= */
/* Wordt gebruikt op admin-, foutpagina's en contactformulier, zodat de login-knop (.btn) ongemoeid blijft. */
.btn-pro{
  display:inline-flex; align-items:center; justify-content:center;
  gap:.4rem;
  padding:.6rem 1rem;
  font-size:.9rem; font-weight:600; line-height:1;
  color:var(--text);
  background:color-mix(in oklab, var(--surface) 88%, var(--brand) 12%);
  border:1px solid color-mix(in oklab, var(--line) 70%, var(--brand) 30%);
  border-radius:10px;
  text-decoration:none !important;
  cursor:pointer;
  box-shadow:0 1px 2px rgba(15,23,42,.06), 0 2px 6px rgba(15,23,42,.04);
  transition:background .15s, border-color .15s, box-shadow .15s, transform .02s, color .15s;
  appearance:none;
  -webkit-appearance:none;
}
.btn-pro:hover{
  background:color-mix(in oklab, var(--surface) 75%, var(--brand) 25%);
  border-color:color-mix(in oklab, var(--line) 40%, var(--brand) 60%);
  color:var(--brand);
  text-decoration:none !important;
  box-shadow:0 2px 4px rgba(15,23,42,.08), 0 4px 12px rgba(15,76,152,.12);
}
.btn-pro:active{ transform:translateY(1px); }
.btn-pro:focus-visible{
  outline:none;
  box-shadow:0 0 0 3px color-mix(in oklab, var(--ring) 35%, transparent);
}

/* Primaire variant: gevuld, voor accent-acties (bv. "Aanmaken") */
.btn-pro.primary{
  color:#fff;
  background:linear-gradient(180deg, var(--brand), color-mix(in oklab, var(--brand) 88%, black 12%));
  border-color:color-mix(in oklab, var(--brand) 80%, black 20%);
  box-shadow:0 1px 2px rgba(15,76,152,.25), 0 2px 8px rgba(15,76,152,.18);
}
.btn-pro.primary:hover{
  color:#fff;
  background:linear-gradient(180deg, color-mix(in oklab, var(--brand) 92%, white 8%), var(--brand));
  border-color:color-mix(in oklab, var(--brand) 70%, black 30%);
  box-shadow:0 2px 4px rgba(15,76,152,.28), 0 6px 16px rgba(15,76,152,.22);
}

/* Secundair (subtiel): identiek aan basis .btn-pro, expliciete class voor leesbaarheid */
.btn-pro.secondary{
  color:var(--text);
  background:color-mix(in oklab, var(--surface) 92%, var(--brand) 8%);
  border-color:var(--line);
}
.btn-pro.secondary:hover{
  background:color-mix(in oklab, var(--surface) 80%, var(--brand) 20%);
  color:var(--brand);
  border-color:color-mix(in oklab, var(--line) 50%, var(--brand) 50%);
}

/* Gevaar-variant: voor "Verwijderen" */
.btn-pro.danger{
  color:#b91c1c;
  background:color-mix(in oklab, var(--surface) 92%, #ef4444 8%);
  border-color:color-mix(in oklab, var(--line) 55%, #ef4444 45%);
}
.btn-pro.danger:hover{
  color:#fff;
  background:linear-gradient(180deg, #ef4444, #b91c1c);
  border-color:#991b1b;
  box-shadow:0 2px 4px rgba(185,28,28,.25), 0 6px 16px rgba(185,28,28,.18);
}

/* Kleine variant voor dichtbevolkte toolbars */
.btn-pro.sm{ padding:.45rem .75rem; font-size:.82rem; }

/* Dark mode: iets transparanter zodat het past op glas-kaarten */
@media (prefers-color-scheme: dark){
  .btn-pro{
    color:var(--text);
    background:color-mix(in oklab, var(--surface-2) 70%, var(--brand) 15%);
    border-color:color-mix(in oklab, var(--line) 60%, var(--brand) 40%);
  }
  .btn-pro:hover{
    background:color-mix(in oklab, var(--surface-2) 55%, var(--brand) 30%);
    color:#fff;
  }
  .btn-pro.secondary{
    background:color-mix(in oklab, var(--surface-2) 80%, var(--brand) 10%);
    border-color:var(--line);
  }
}

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
# Wordt per request gerenderd zodat elke tenant zijn eigen favicon krijgt op
# basis van current_brand(). Als de tenant een eigen BRAND_LOGO_SVG heeft,
# wordt die rechtstreeks teruggestuurd; anders een initialen-badge.
def _render_favicon_svg() -> str:
    try:
        b = current_brand()
    except Exception:
        b = {"short": "FT", "color": "", "logo_svg": ""}
    # Eigen logo? Stuur dat onveranderd terug.
    if b.get("logo_svg"):
        return b["logo_svg"]
    # Maximaal 2 letters in de favicon; 3 letters wordt op browser-tabformaat
    # vrijwel onleesbaar. current_brand() bepaalt per ingelogde user/tenant de
    # juiste branding, en deze extra slice voorkomt dat oudere DB/env waarden
    # alsnog 3 letters tonen.
    short = (b.get("short") or "FT")[:2]
    color = (b.get("color") or "#1E3A8A").strip() or "#1E3A8A"
    # XML-escape de letters voor het geval iemand iets geks invult.
    from xml.sax.saxutils import escape as _xml_escape
    short_safe = _xml_escape(short)
    # Groot genoeg voor 1-2 letters op tabformaat.
    fs = 32
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">'
        f'<rect width="64" height="64" rx="12" fill="{color}"/>'
        f'<text x="50%" y="55%" text-anchor="middle" dominant-baseline="middle" '
        f'font-family="Segoe UI, Roboto, sans-serif" font-size="{fs}" font-weight="700" '
        f'fill="white">{short_safe}</text>'
        f'</svg>'
    )

@app.route("/favicon.svg")
def favicon_svg():
    resp = Response(_render_favicon_svg(), mimetype="image/svg+xml")
    # Favicons worden agressief gecachet door browsers. Omdat deze favicon
    # per user/tenant verschilt, voorkomen we dat bv. de oude OHB-favicon
    # zichtbaar blijft na wisselen van account of tenant.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp

@app.route("/favicon.ico")
def favicon_ico():
    # browsers die /favicon.ico hardcoderen -> redirect naar svg
    return redirect(url_for("favicon_svg"), code=302)

# -------------- Templates --------------
BG_DIV = '<div class="bg" aria-hidden="true"></div>'
# Het favicon wordt per request gerenderd door /favicon.svg, dus we verwijzen
# alleen naar die URL en bedden de SVG niet langer als data-URL in.
# NB: hardcoded paden i.p.v. {{ url_for(...) }}, omdat deze string via
# {{ head_icon|safe }} in templates wordt geïnjecteerd. |safe markeert de string
# als reeds-gerenderde HTML, dus Jinja zou de url_for-expressies niet nog eens
# evalueren — browsers kregen dan letterlijk "{{ url_for(...) }}" als href en
# laadden geen favicon. /favicon.svg en /favicon.ico zijn vaste app-routes.
HTML_HEAD_ICON = """
<link rel="icon" href="/favicon.svg" type="image/svg+xml"/>
<link rel="alternate icon" href="/favicon.svg" type="image/svg+xml"/>
<link rel="shortcut icon" href="/favicon.ico"/>
"""

LOGIN_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen – {{ brand.name }}</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card" style="max-width:460px;margin:auto">
  <h1 style="color:var(--brand)">Inloggen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}

<form method="post" autocomplete="off">
  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
  <!-- honeypots tegen automated bots -->
  <input type="text" name="x" style="display:none">
  <input type="password" name="y" style="display:none" autocomplete="new-password">

  <label for="email">E-mail</label>
  <input id="email" class="input" name="email" type="email"
         value="{{ auth_email }}" autocomplete="username" required>

  <label for="password">Wachtwoord</label>
  <input id="password" class="input" type="password" name="password"
         placeholder="Wachtwoord" required
         autocomplete="current-password"
         autocapitalize="off"
         autocorrect="off"
         spellcheck="false">

  <button class="btn" type="submit" style="margin-top:1rem;width:100%">Inloggen</button>
</form>

  {% set _trial_url = trial_signup_url() %}
  {% if _trial_url %}
  <p class="footer small" style="margin-top:1rem;text-align:center">
    Nog geen account? <a href="{{ _trial_url }}">Probeer de gratis variant</a>.
  </p>
  {% endif %}

  <p class="footer small">{{ brand.footer }}</p>
</div></div>
</body></html>
"""

PASS_PROMPT_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Beveiligd · {{ brand.name }}</title>{{ head_icon|safe }}
<style>
{{ base_css }}
:root{
  --oh-bg:#f4f6fa; --oh-surface:#fff; --oh-surface-2:#f8fafc;
  --oh-border:#e2e8f0; --oh-border-strong:#cbd5e1;
  --oh-text:#0f172a; --oh-muted:#64748b;
  --oh-brand:#0f3a6b; --oh-brand-2:#1e5a9e; --oh-accent:#d97706;
  --oh-danger:#dc2626; --oh-radius:10px; --oh-radius-sm:6px;
  --oh-shadow:0 1px 3px rgba(15,23,42,.06), 0 8px 24px rgba(15,23,42,.04);
}
*,*::before,*::after{box-sizing:border-box}
html,body{min-height:100%;margin:0;background:var(--oh-bg);color:var(--oh-text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",Roboto,sans-serif;
  font-size:15px;line-height:1.5;display:grid;place-items:center;padding:24px}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background-image:
    radial-gradient(circle at 20% 10%, rgba(217,119,6,.04), transparent 40%),
    radial-gradient(circle at 80% 80%, rgba(15,58,107,.05), transparent 45%);}
.pw-card{position:relative;z-index:1;width:100%;max-width:440px;
  background:var(--oh-surface);border:1px solid var(--oh-border);
  border-radius:var(--oh-radius);box-shadow:var(--oh-shadow);padding:28px}
.pw-brand{display:flex;align-items:center;gap:12px;margin-bottom:18px}
.pw-brand-mark{width:38px;height:38px;border-radius:8px;
  background:linear-gradient(135deg,var(--oh-brand),var(--oh-brand-2));
  display:grid;place-items:center;color:#fff;font-weight:700;font-size:14px;
  box-shadow:inset 0 -2px 0 rgba(0,0,0,.15);overflow:hidden}
.pw-brand-mark svg{width:100%;height:100%;display:block}
.pw-brand-text h1{margin:0;font-size:16px;font-weight:600}
.pw-brand-text p{margin:0;font-size:12px;color:var(--oh-muted)}
.pw-lock{width:56px;height:56px;border-radius:50%;
  background:#fef3c7;color:var(--oh-accent);
  display:grid;place-items:center;margin:4px auto 14px}
.pw-title{text-align:center;font-size:18px;font-weight:600;margin:0 0 4px}
.pw-sub{text-align:center;font-size:13px;color:var(--oh-muted);margin:0 0 20px}
.pw-err{background:#fee2e2;color:#991b1b;border:1px solid #fecaca;
  padding:10px 14px;border-radius:var(--oh-radius-sm);font-size:14px;margin-bottom:14px}
.pw-label{display:block;font-size:12px;font-weight:600;text-transform:uppercase;
  letter-spacing:.04em;color:var(--oh-muted);margin-bottom:6px}
.pw-input{width:100%;padding:10px 12px;background:var(--oh-surface);
  border:1px solid var(--oh-border-strong);border-radius:var(--oh-radius-sm);
  font:inherit;color:var(--oh-text);transition:border-color .15s,box-shadow .15s}
.pw-input:focus{outline:none;border-color:var(--oh-brand-2);
  box-shadow:0 0 0 3px rgba(30,90,158,.12)}
.pw-btn{width:100%;margin-top:14px;padding:11px;background:var(--oh-brand);
  color:#fff;border:none;border-radius:var(--oh-radius-sm);font:inherit;
  font-weight:600;cursor:pointer;transition:background .15s}
.pw-btn:hover{background:var(--oh-brand-2)}
.pw-footer{text-align:center;font-size:12px;color:var(--oh-muted);margin-top:18px}

@media (prefers-color-scheme: dark){
  :root{
    --oh-bg:#0f172a; --oh-surface:#1e293b; --oh-surface-2:#0f172a;
    --oh-border:#334155; --oh-border-strong:#475569;
    --oh-text:#f1f5f9; --oh-muted:#94a3b8;
    --oh-brand:#60a5fa; --oh-brand-2:#3b82f6;
  }
  .pw-lock{background:#422006;color:#fde68a}
  .pw-err{background:#450a0a;color:#fca5a5;border-color:#991b1b}
}
</style></head><body>

<div class="pw-card">
  <div class="pw-brand">
    <div class="pw-brand-mark"{% if brand.color %} style="background:{{ brand.color }}"{% endif %}>
      {% if brand.logo_svg %}{{ brand.logo_svg|safe }}{% else %}{{ brand.short }}{% endif %}
    </div>
    <div class="pw-brand-text">
      <h1>{{ brand.name }}</h1>
      <p>Bestandentransfer</p>
    </div>
  </div>

  <div class="pw-lock">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
  </div>

  <h2 class="pw-title">Beveiligd pakket</h2>
  <p class="pw-sub">Voer het wachtwoord in om dit pakket te openen</p>

  {% if error %}<div class="pw-err">{{ error }}</div>{% endif %}

  <form method="post" autocomplete="off">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="text" name="a" style="display:none"><input type="password" name="b" style="display:none">
    <label class="pw-label" for="pw">Wachtwoord</label>
    <input id="pw" class="pw-input" type="password" name="password" placeholder="Voer wachtwoord in"
           required autocomplete="new-password" autocapitalize="off" spellcheck="false" autofocus>
    <button class="pw-btn" type="submit">Ontgrendel pakket</button>
  </form>

  <p class="pw-footer">{{ brand.footer }}</p>
</div>
</body></html>
"""

INDEX_HTML = """
<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <meta name="csrf-token" content="{{ csrf_token() }}"/>
  <title>Uploaden • {{ brand.name }}</title>
  {{ head_icon|safe }}
  <style>
    {{ base_css }}

    /* =============== Professioneel ontwerp =============== */
    :root{
      /* Achtergrond-tinten worden nu overgelaten aan BASE_CSS (aurora).
         Deze custom vars blijven voor de LOCALE component-styling. */
      --oh-surface: rgba(255,255,255,.82);
      --oh-surface-2: rgba(248,250,252,.70);
      --oh-border: rgba(148,163,184,.35);
      --oh-border-strong: rgba(148,163,184,.55);
      --oh-text: #0f172a;
      --oh-muted: #475569;
      --oh-brand: #0f3a6b;         /* navy — serieus, bouw-gevoel */
      --oh-brand-2: #1e5a9e;
      --oh-accent: #d97706;        /* warm oranje — bouw-accent, signaal-kleur */
      --oh-accent-2: #f59e0b;
      --oh-success: #16a34a;
      --oh-danger: #dc2626;
      --oh-radius: 10px;
      --oh-radius-sm: 6px;
      --oh-shadow: 0 1px 3px rgba(15,23,42,.06), 0 18px 40px rgba(15,23,42,.10);
      --oh-shadow-hover: 0 4px 12px rgba(15,23,42,.12), 0 24px 48px rgba(15,23,42,.14);
    }
    @media (prefers-color-scheme: dark){
      :root{
        --oh-surface: rgba(17,24,39,.70);
        --oh-surface-2: rgba(15,23,42,.55);
        --oh-border: rgba(148,163,184,.22);
        --oh-border-strong: rgba(148,163,184,.38);
        --oh-text: #e5e7eb;
        --oh-muted: #9aa3b2;
        --oh-brand: #7db4ff;
        --oh-brand-2: #4a7fff;
        --oh-shadow: 0 1px 3px rgba(0,0,0,.35), 0 18px 40px rgba(0,0,0,.45);
      }
    }

    *, *::before, *::after { box-sizing: border-box; }

    /* Body-achtergrond transparant laten zodat de aurora (van BASE_CSS .bg)
       doorheen schijnt. Géén eigen body::before meer die de aurora verbergt. */
    html, body {
      min-height: 100%;
      background: transparent;
      color: var(--oh-text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Inter", Roboto, sans-serif;
      font-size: 15px;
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      margin: 0;
    }

    .oh-shell {
      position: relative;
      z-index: 1;
      max-width: 1200px;
      margin: 0 auto;
      padding: 28px 22px 60px;
    }

    /* ============ Top bar ============ */
    .oh-topbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 20px;
      padding: 14px 20px;
      background: var(--oh-surface);
      backdrop-filter: blur(10px) saturate(1.05);
      -webkit-backdrop-filter: blur(10px) saturate(1.05);
      border: 1px solid var(--oh-border);
      border-radius: var(--oh-radius);
      box-shadow: var(--oh-shadow);
      margin-bottom: 22px;
      flex-wrap: wrap;
    }
    .oh-brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .oh-brand-mark {
      width: 38px; height: 38px;
      border-radius: 8px;
      background: linear-gradient(135deg, var(--oh-brand), var(--oh-brand-2));
      display: grid; place-items: center;
      color: white;
      font-weight: 700;
      font-size: 14px;
      letter-spacing: .5px;
      box-shadow: inset 0 -2px 0 rgba(0,0,0,.15);
      overflow: hidden;
    }
    .oh-brand-mark svg {
      width: 100%;
      height: 100%;
      display: block;
    }
    .oh-brand-text h1 {
      margin: 0;
      font-size: 17px;
      font-weight: 600;
      letter-spacing: -0.01em;
      color: var(--oh-text);
    }
    .oh-brand-text p {
      margin: 0;
      font-size: 12px;
      color: var(--oh-muted);
    }
.oh-userbar {
  display:flex;
  align-items:center;
  gap:12px;
  font-size:13px;
  color:var(--oh-text);
  flex-wrap:wrap;
}

.oh-userbar strong{
  color:var(--oh-text);
  font-weight:700;
}

.oh-userbar a{
  color:var(--oh-brand-2);
  text-decoration:none;
  padding:7px 12px;
  border-radius:var(--oh-radius-sm);
  background:var(--oh-surface-2);
  border:1px solid var(--oh-border);
  transition:background .15s, color .15s, border-color .15s;
}

.oh-userbar a:hover{
  background:var(--oh-surface);
  color:var(--oh-brand);
  border-color:var(--oh-brand-2);
}

    /* ============ Deck / layout ============ */
    .oh-deck {
      display: grid;
      grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
      gap: 22px;
    }
    @media (max-width: 880px){
      .oh-deck { grid-template-columns: 1fr; }
    }

    /* ============ Card ============ */
    .oh-card {
      background: var(--oh-surface);
      border: 1px solid var(--oh-border);
      border-radius: var(--oh-radius);
      box-shadow: var(--oh-shadow);
      overflow: hidden;
    }
    .oh-card-head {
      padding: 18px 22px;
      border-bottom: 1px solid var(--oh-border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      background: var(--oh-surface-2);
    }
    .oh-card-head h2 {
      margin: 0;
      font-size: 15px;
      font-weight: 600;
      letter-spacing: -0.005em;
      color: var(--oh-text);
      display: flex; align-items: center; gap: 10px;
    }
    .oh-card-head h2 svg { color: var(--oh-brand); }
    .oh-card-body { padding: 22px; }

    /* ============ Formulier ============ */
    .oh-grid {
      display: grid;
      gap: 16px;
    }
    .oh-grid.cols2 {
      grid-template-columns: 1fr 1fr;
    }
    @media (max-width: 640px){
      .oh-grid.cols2 { grid-template-columns: 1fr; }
    }
    .oh-label {
      display: block;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: var(--oh-muted);
      margin-bottom: 6px;
    }
    .oh-input,
    .oh-select {
      width: 100%;
      padding: 10px 12px;
      background: var(--oh-surface);
      border: 1px solid var(--oh-border-strong);
      border-radius: var(--oh-radius-sm);
      font: inherit;
      color: var(--oh-text);
      transition: border-color .15s, box-shadow .15s;
    }
    .oh-input:focus,
    .oh-select:focus {
      outline: none;
      border-color: var(--oh-brand-2);
      box-shadow: 0 0 0 3px rgba(30, 90, 158, .12);
    }
    .oh-input::placeholder { color: #94a3b8; }

    /* Toggle radios */
    .oh-toggle {
      display: inline-flex;
      background: var(--oh-surface-2);
      border: 1px solid var(--oh-border);
      border-radius: var(--oh-radius-sm);
      padding: 3px;
      gap: 2px;
    }
    .oh-toggle label {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 7px 14px;
      cursor: pointer;
      font-size: 13px;
      color: var(--oh-muted);
      border-radius: 4px;
      transition: background .15s, color .15s;
      user-select: none;
    }
    .oh-toggle label:hover { color: var(--oh-text); }
    .oh-toggle input { display: none; }
    .oh-toggle label:has(input:checked) {
      background: var(--oh-surface);
      color: var(--oh-brand);
      box-shadow: 0 1px 2px rgba(15,23,42,.08);
      font-weight: 600;
    }

    /* ============ Drop zone ============ */
    .oh-drop {
      position: relative;
      border: 2px dashed var(--oh-border-strong);
      border-radius: var(--oh-radius);
      padding: 32px 20px;
      text-align: center;
      background: var(--oh-surface-2);
      transition: border-color .15s, background .15s;
      cursor: pointer;
    }
    .oh-drop:hover {
      border-color: var(--oh-brand-2);
      background: #f1f5f9;
    }
    .oh-drop.dragover {
      border-color: var(--oh-accent);
      background: #fff7ed;
    }
    .oh-drop-icon {
      display: inline-flex;
      width: 48px; height: 48px;
      border-radius: 50%;
      background: var(--oh-surface);
      border: 1px solid var(--oh-border);
      align-items: center; justify-content: center;
      margin-bottom: 12px;
      color: var(--oh-brand);
    }
    .oh-drop-title {
      font-weight: 600;
      color: var(--oh-text);
      margin-bottom: 4px;
    }
    .oh-drop-sub {
      font-size: 13px;
      color: var(--oh-muted);
      margin-bottom: 12px;
    }
    .oh-drop-pick {
      display: inline-block;
      padding: 7px 14px;
      background: var(--oh-surface);
      border: 1px solid var(--oh-border-strong);
      border-radius: var(--oh-radius-sm);
      font-weight: 600;
      font-size: 13px;
      color: var(--oh-brand);
      transition: background .15s;
    }
    .oh-drop-pick:hover { background: var(--oh-surface-2); }

    .oh-drop input[type=file] {
      position: absolute;
      inset: 0;
      opacity: 0;
      cursor: pointer;
    }
    .oh-drop-filename {
      margin-top: 10px;
      font-size: 13px;
      color: var(--oh-muted);
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 100%;
    }
    .oh-drop-filename.has-file { color: var(--oh-text); }

    /* ============ Knoppen ============ */
    .oh-btn {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 20px;
      background: var(--oh-brand);
      color: white;
      border: none;
      border-radius: var(--oh-radius-sm);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
      transition: background .15s, transform .05s;
      text-decoration: none;
    }
    .oh-btn:hover:not(:disabled) { background: var(--oh-brand-2); }
    .oh-btn:active:not(:disabled) { transform: translateY(1px); }
    .oh-btn:disabled {
      opacity: .5;
      cursor: not-allowed;
    }
    .oh-btn.ghost {
      background: var(--oh-surface);
      color: var(--oh-brand);
      border: 1px solid var(--oh-border-strong);
    }
    .oh-btn.ghost:hover:not(:disabled) {
      background: var(--oh-surface-2);
      border-color: var(--oh-brand-2);
    }
    .oh-btn.accent {
      background: var(--oh-accent);
    }
    .oh-btn.accent:hover:not(:disabled) { background: var(--oh-accent-2); }

    /* ============ Action row ============ */
    .oh-actionrow {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding-top: 6px;
      flex-wrap: wrap;
    }
    .oh-meta {
      font-size: 13px;
      color: var(--oh-muted);
    }
    .oh-meta .pill {
      display: inline-block;
      padding: 2px 8px;
      background: var(--oh-surface-2);
      border: 1px solid var(--oh-border);
      border-radius: 999px;
      font-variant-numeric: tabular-nums;
      font-weight: 600;
      color: var(--oh-text);
      margin: 0 2px;
    }

    /* ============ Progress & queue ============ */
    .oh-queue {
      margin-top: 16px;
      display: grid;
      gap: 8px;
      max-height: 260px;
      overflow-y: auto;
    }
    .oh-queue-item {
      display: grid;
      grid-template-columns: 20px 1fr auto auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      background: var(--oh-surface-2);
      border: 1px solid var(--oh-border);
      border-radius: var(--oh-radius-sm);
      font-size: 13px;
    }
    .oh-queue-item .name {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      color: var(--oh-text);
    }
    .oh-queue-item .size {
      color: var(--oh-muted);
      font-variant-numeric: tabular-nums;
    }
    .oh-queue-item .state {
      font-weight: 600;
      font-size: 12px;
      color: var(--oh-muted);
    }
    .oh-queue-item.done .state { color: var(--oh-success); }
    .oh-queue-item.err .state { color: var(--oh-danger); }
    .oh-queue-item.active .state { color: var(--oh-accent); }
    .oh-queue-item .dot {
      width: 8px; height: 8px; border-radius: 50%;
      background: var(--oh-border-strong);
    }
    .oh-queue-item.active .dot {
      background: var(--oh-accent);
      animation: oh-pulse 1.4s ease-in-out infinite;
    }
    .oh-queue-item.done .dot { background: var(--oh-success); }
    .oh-queue-item.err .dot { background: var(--oh-danger); }

    @keyframes oh-pulse {
      0%, 100% { opacity: 1; transform: scale(1); }
      50%      { opacity: .5; transform: scale(.8); }
    }

    /* Totaal-balk */
    .oh-total-head {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      margin-top: 20px;
      margin-bottom: 8px;
    }
    .oh-total-head span:first-child {
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .04em;
      color: var(--oh-muted);
    }
    .oh-total-pct {
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--oh-brand);
      font-size: 15px;
    }
    .oh-bar {
      height: 8px;
      background: var(--oh-surface-2);
      border: 1px solid var(--oh-border);
      border-radius: 999px;
      overflow: hidden;
    }
    .oh-bar > i {
      display: block;
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--oh-brand), var(--oh-brand-2));
      transition: width .25s ease;
      border-radius: 999px;
      position: relative;
    }
    /* Subtiele glim-animatie bij actief uploaden */
    .oh-bar.active > i::after {
      content: "";
      position: absolute;
      inset: 0;
      background: linear-gradient(90deg,
        transparent 0%, rgba(255,255,255,.35) 50%, transparent 100%);
      animation: oh-shine 1.6s linear infinite;
    }
    @keyframes oh-shine {
      0%   { transform: translateX(-100%); }
      100% { transform: translateX(100%); }
    }
    .oh-total-status {
      margin-top: 8px;
      font-size: 13px;
      color: var(--oh-muted);
    }

    /* ============ Share result ============ */
    .oh-share {
      margin-top: 18px;
      padding: 18px;
      background: linear-gradient(135deg, #f0f9ff, #ecfeff);
      border: 1px solid #bae6fd;
      border-radius: var(--oh-radius);
    }
    .oh-share-head {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 10px;
      font-weight: 600;
      color: #075985;
    }
    .oh-share-head svg { color: var(--oh-success); }
    .oh-share-row {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    .oh-share-row .oh-input {
      flex: 1 1 auto;
      min-width: 200px;
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 14px;
      background: #ffffff;
      color: #0f172a;
      border-color: #bae6fd;
      font-weight: 500;
      letter-spacing: .01em;
    }
    .oh-share-row .oh-input:focus {
      color: #0f172a;
      background: #ffffff;
    }

    /* ============ Telemetry ============ */
    .oh-stats {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 12px;
      margin-bottom: 20px;
    }
    .oh-stat {
      padding: 12px 14px;
      background: var(--oh-surface-2);
      border: 1px solid var(--oh-border);
      border-radius: var(--oh-radius-sm);
    }
    .oh-stat .k {
      font-size: 11px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: .05em;
      color: var(--oh-muted);
      margin-bottom: 4px;
    }
    .oh-stat .v {
      font-size: 16px;
      font-weight: 600;
      color: var(--oh-text);
      font-variant-numeric: tabular-nums;
    }

    .oh-log {
      font-family: ui-monospace, "SF Mono", Menlo, monospace;
      font-size: 12px;
      background: #0f172a;
      color: #cbd5e1;
      border-radius: var(--oh-radius-sm);
      padding: 12px 14px;
      max-height: 220px;
      overflow-y: auto;
      line-height: 1.55;
    }
    .oh-log .t { color: #64748b; margin-right: 6px; }
    .oh-log .ok { color: #86efac; }
    .oh-log .err { color: #fca5a5; }
    .oh-log .warn { color: #fcd34d; }

    /* ============ Notice (kleine speelse touch) ============ */
    .oh-notice {
      display: flex;
      gap: 10px;
      padding: 10px 14px;
      background: #fffbeb;
      border: 1px solid #fde68a;
      border-radius: var(--oh-radius-sm);
      color: #78350f;
      font-size: 13px;
      margin-bottom: 16px;
    }
    .oh-notice svg {
      flex-shrink: 0;
      color: var(--oh-accent);
    }

    /* ============ Footer ============ */
    .oh-footer {
      margin-top: 32px;
      padding-top: 18px;
      border-top: 1px solid var(--oh-border);
      text-align: center;
      font-size: 12px;
      color: var(--oh-muted);
    }
    .oh-footer a {
      color: var(--oh-brand-2);
      text-decoration: none;
      margin: 0 6px;
    }
    .oh-footer a:hover { text-decoration: underline; }
  </style>
</head>
<body>
{{ bg|safe }}

<div class="oh-shell">

  <!-- Top bar -->
  <header class="oh-topbar">
    <div class="oh-brand">
      <div class="oh-brand-mark"{% if brand.color %} style="background:{{ brand.color }}"{% endif %}>
        {% if brand.logo_svg %}{{ brand.logo_svg|safe }}{% else %}{{ brand.short }}{% endif %}
      </div>
      <div class="oh-brand-text">
        <h1>{{ brand.name }}</h1>
        <p>{{ brand.tagline }}</p>
      </div>
    </div>
    <div class="oh-userbar">
      <span>Ingelogd als <strong>{{ user }}</strong></span>
      <a href="/uploads">Mijn uploads</a>
      {% if is_admin %}<a href="/admin/users">Beheer</a>{% endif %}
      <a href="{{ url_for('logout') }}">Uitloggen</a>
    </div>
  </header>

  <div class="oh-deck">

    <!-- ============ Upload card ============ -->
    <section class="oh-card">
      <div class="oh-card-head">
        <h2>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Uploaden
        </h2>
        <span class="oh-meta">Parallel: <span class="pill" id="kvWorkers">3</span></span>
      </div>

      <div class="oh-card-body">

        <form id="form" class="oh-grid" autocomplete="off" enctype="multipart/form-data">

          <div class="oh-grid cols2">
            <div>
              <label class="oh-label">Uploadtype</label>
              <div class="oh-toggle">
                <label><input type="radio" name="upmode" value="files" checked> Bestand(en)</label>
                <label id="folderLabel"><input type="radio" name="upmode" value="folder"> Map</label>
              </div>
            </div>
            <div>
              <label class="oh-label" for="title">Onderwerp <span style="color:var(--oh-muted);font-weight:400;text-transform:none;letter-spacing:normal">(optioneel)</span></label>
              <input id="title" class="oh-input" type="text" placeholder="Bijv. Tekeningen project X" maxlength="120">
            </div>
          </div>

          <div class="oh-grid cols2">
            <div>
              <label class="oh-label" for="expDays">Verloopt na</label>
              <select id="expDays" class="oh-select">
                <option value="1">1 dag</option>
                <option value="3"{% if is_trial %} selected{% endif %}>3 dagen{% if is_trial %} (max trial){% endif %}</option>
                <option value="7"{% if is_trial %} disabled{% endif %}>7 dagen{% if is_trial %} — betaald{% endif %}</option>
                <option value="14"{% if is_trial %} disabled{% endif %}>14 dagen{% if is_trial %} — betaald{% endif %}</option>
                <option value="30"{% if is_trial %} disabled{% else %} selected{% endif %}>30 dagen{% if is_trial %} — betaald{% endif %}</option>
                <option value="60"{% if is_trial %} disabled{% endif %}>60 dagen{% if is_trial %} — betaald{% endif %}</option>
                <option value="90"{% if is_trial %} disabled{% endif %}>90 dagen{% if is_trial %} — betaald{% endif %}</option>
                <option value="never"{% if is_trial %} disabled{% endif %}>Onbeperkt geldig{% if is_trial %} — betaald{% endif %}</option>
              </select>
              {% if is_trial %}
              <p style="margin:.4rem 0 0;font-size:.8rem;color:var(--oh-muted);line-height:1.4">
                Trial-pakketten zijn maximaal <strong>3 dagen</strong> geldig.
                Langere bewaartermijnen zitten in de
                {% set _signup_url = trial_signup_url() %}{% if _signup_url %}<a href="/contact" style="color:inherit;text-decoration:underline">betaalde variant</a>{% else %}betaalde variant{% endif %}.
              </p>
              {% endif %}
            </div>
            <div>
              <label class="oh-label" for="pw">Wachtwoord <span style="color:var(--oh-muted);font-weight:400;text-transform:none;letter-spacing:normal">(optioneel)</span></label>
              <input id="pw" class="oh-input" type="password"
                     placeholder="{% if is_trial %}Wachtwoord-bescherming: betaald{% else %}Leeg = geen wachtwoord{% endif %}"
                     autocomplete="new-password"{% if is_trial %} disabled{% endif %}>
              {% if is_trial %}
              <p style="margin:.4rem 0 0;font-size:.8rem;color:var(--oh-muted);line-height:1.4">
                Wachtwoord-bescherming zit in de betaalde variant.
              </p>
              {% endif %}
            </div>
          </div>

          <!-- Drop zone for files -->
          <div id="fileRow">
            <label class="oh-label">Bestanden</label>
            <div class="oh-drop" id="dropFiles">
              <div class="oh-drop-icon">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
              </div>
              <div class="oh-drop-title">Sleep bestanden hierheen</div>
              <div class="oh-drop-sub">of</div>
              <span class="oh-drop-pick" id="btnFiles">Kies bestanden</span>
              <div class="oh-drop-filename" id="fileName">Nog geen bestanden gekozen</div>
              <input id="fileInput" type="file" multiple>
            </div>
          </div>

          <!-- Drop zone for folder -->
          <div id="folderRow" style="display:none">
            <label class="oh-label">Map</label>
            <div class="oh-drop" id="dropFolder">
              <div class="oh-drop-icon">
                <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
              </div>
              <div class="oh-drop-title">Selecteer een map</div>
              <div class="oh-drop-sub">de mapstructuur blijft behouden</div>
              <span class="oh-drop-pick" id="btnFolder">Kies map</span>
              <div class="oh-drop-filename" id="folderName">Nog geen map gekozen</div>
              <div class="oh-drop-hint" id="folderHint" style="display:none;font-size:.78rem;color:var(--oh-muted);margin-top:.5rem;line-height:1.4;max-width:380px">
                Op sommige Android-browsers werkt mapselectie beperkt. Lukt het niet?
                Schakel terug naar <strong>Bestand(en)</strong> en selecteer alle bestanden tegelijk.
              </div>
              <input id="folderInput" type="file" multiple webkitdirectory directory>
            </div>
          </div>

          <!-- Action row -->
          <div class="oh-actionrow">
            <button id="btnStart" class="oh-btn" type="submit">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>
              Uploaden
            </button>
            <span class="oh-meta">Queue: <span class="pill" id="kvQueue">0</span>  Bestanden: <span class="pill" id="kvFiles">0</span></span>
          </div>
        </form>

        <div id="queue" class="oh-queue"></div>

        <div class="oh-total-head">
          <span>Totaalvoortgang</span>
          <span class="oh-total-pct" id="totalPct">0%</span>
        </div>
        <div class="oh-bar" id="totalBar"><i id="totalFill"></i></div>
        <div class="oh-total-status" id="totalStatus">Nog niet gestart</div>

        <div id="result"></div>
      </div>
    </section>

    <!-- ============ Telemetry card ============ -->
    <aside class="oh-card">
      <div class="oh-card-head">
        <h2>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
          Live status
        </h2>
        <span class="oh-meta">Sessie</span>
      </div>
      <div class="oh-card-body">
        <div class="oh-stats">
          <div class="oh-stat">
            <div class="k">Actieve workers</div>
            <div class="v" id="tWorkers">0</div>
          </div>
          <div class="oh-stat">
            <div class="k">Doorvoersnelheid</div>
            <div class="v"><span id="tSpeed">0</span><span style="font-size:12px;color:var(--oh-muted);font-weight:400"> /s</span></div>
          </div>
          <div class="oh-stat">
            <div class="k">Verplaatst</div>
            <div class="v" id="tMoved">0 B</div>
          </div>
          <div class="oh-stat">
            <div class="k">Resterend</div>
            <div class="v" id="tLeft">0 B</div>
          </div>
          <div class="oh-stat">
            <div class="k">ETA</div>
            <div class="v" id="tEta">—</div>
          </div>
          <div class="oh-stat">
            <div class="k">Bestanden klaar</div>
            <div class="v" id="tDone">0</div>
          </div>
        </div>

        <label class="oh-label" style="margin-bottom:8px">Activiteitenlog</label>
        <div id="log" class="oh-log" aria-live="polite"></div>
      </div>
    </aside>
  </div>

  <footer class="oh-footer">
    {{ brand.footer }}
    <span style="margin:0 6px;color:var(--oh-border-strong)">|</span>
    <a href="{{ url_for('terms_page') }}">Voorwaarden</a>
    <a href="{{ url_for('privacy_page') }}">Privacy</a>
  </footer>
</div>

<script>
/* ==== Settings & platform-detectie ==== */
const FILE_PAR = 3;
const isIOS = /iPad|iPhone|iPod/.test(navigator.userAgent)||(navigator.platform==='MacIntel'&&navigator.maxTouchPoints>1);
const isAndroid = /Android/i.test(navigator.userAgent);
// Feature-detectie voor mappen-picker. Op iOS Safari/Chrome bestaat dit
// hele concept niet — daar verbergen we de knop. Op Android werkt het in
// de praktijk wisselend: feature-detect geeft true terug maar sommige
// browsers tonen alsnog een bestand-picker. Daar tonen we de knop met
// een hint, en geven we bij een lege selectie een duidelijke foutmelding.
const supportsFolderPicker = (() => {
  try {
    const t = document.createElement('input');
    return ('webkitdirectory' in t) && !isIOS;
  } catch(e) { return false; }
})();

/* CSRF token uit <meta name="csrf-token"> */
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]')||{}).content || '';
function jsonHeaders(){ return {"Content-Type":"application/json","X-CSRF-Token":CSRF_TOKEN}; }

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

if(!supportsFolderPicker){ folderLabel.style.display='none'; }
// Op Android tonen we een korte hint dat mapselectie soms beperkt werkt.
if(supportsFolderPicker && isAndroid){
  const hint = document.getElementById('folderHint');
  if(hint) hint.style.display='block';
}

/* Utils */
function fmtBytes(n){const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):Math.round(n))+" "+u[i]}
function log(msg){const p=document.createElement('p');const t=new Date().toLocaleTimeString();p.textContent=`[${t}] ${msg}`;logEl.prepend(p)}
function setTotal(p,label){
  const pct=Math.max(0,Math.min(100,p));
  totalFill.style.width=pct+'%';
  totalPct.textContent=Math.round(pct)+'%';
  if(label) totalStatus.textContent=label;
  const bar=document.getElementById('totalBar');
  if(bar){
    if(pct>0 && pct<100) bar.classList.add('active');
    else bar.classList.remove('active');
  }
}

/* API */
async function packageInit(expiry,password,title){
  const r=await fetch("{{ url_for('package_init') }}",{method:"POST",headers:jsonHeaders(),body:JSON.stringify({expiry_days:expiry,password,title})});
  const j=await r.json(); if(!j.ok) throw new Error(j.error||'init'); return j.token;
}
async function putInit(token,filename,type){
  const r=await fetch("{{ url_for('put_init') }}",{method:"POST",headers:jsonHeaders(),body:JSON.stringify({token,filename,contentType:type||'application/octet-stream'})});
  const j=await r.json(); if(!j.ok) throw new Error(j.error||'put_init'); return j;
}
async function putComplete(token,key,name,path){
  const r=await fetch("{{ url_for('put_complete') }}",{method:"POST",headers:jsonHeaders(),body:JSON.stringify({token,key,name,path})});
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
  const r=document.createElement('div');
  r.className='oh-queue-item';
  r.innerHTML=`<div class="dot"></div>
               <div class="name" title="${rel}">${rel}</div>
               <div class="size">${fmtBytes(size)}</div>
               <div class="state" data-eta>In wachtrij</div>`;
  queue.appendChild(r);
  return {row:r,fill:null,eta:r.querySelector('[data-eta]')};
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
    const useFolder = (mode === "folder" && supportsFolderPicker);
    if (fileRow) fileRow.style.display = useFolder ? "none" : "";
    if (folderRow) folderRow.style.display = useFolder ? "" : "none";
    // Niet automatisch openen; de drop-zone is zelf klikbaar.
  };

  if (btnFiles && fileInput) btnFiles.addEventListener("click", (e) => { e.stopPropagation(); openPicker(fileInput); });
  if (btnFolder && folderInput) btnFolder.addEventListener("click", (e) => { e.stopPropagation(); openPicker(folderInput); });

  if (fileInput){
    fileInput.addEventListener("change", () => {
      const n = fileInput.files?.length || 0;
      setCounters(n);
      if (fileName) {
        fileName.textContent = fileSummary(fileInput.files, "Nog geen bestanden gekozen");
        fileName.classList.toggle('has-file', n>0);
      }
    });
  }

  if (folderInput){
    folderInput.addEventListener("change", () => {
      const n = folderInput.files?.length || 0;
      setCounters(n);
      if (folderName) {
        folderName.textContent = folderSummary(folderInput.files);
        folderName.classList.toggle('has-file', n>0);
      }
    });
  }

  // ===== Drag & drop ondersteuning =====
  const dropFiles = document.getElementById('dropFiles');
  const dropFolder = document.getElementById('dropFolder');

  function wireDrop(zone, input, isFolder){
    if (!zone || !input) return;
    ['dragenter','dragover'].forEach(ev=>{
      zone.addEventListener(ev, (e)=>{ e.preventDefault(); e.stopPropagation(); zone.classList.add('dragover'); });
    });
    ['dragleave','drop'].forEach(ev=>{
      zone.addEventListener(ev, (e)=>{ e.preventDefault(); e.stopPropagation(); zone.classList.remove('dragover'); });
    });
    zone.addEventListener('drop', (e)=>{
      const dt = e.dataTransfer;
      if (!dt || !dt.files || !dt.files.length) return;
      // DataTransfer files kunnen niet direct in een file-input geplaatst worden, maar
      // moderne browsers ondersteunen input.files = dt.files via DataTransfer.
      try {
        input.files = dt.files;
        input.dispatchEvent(new Event('change', {bubbles:true}));
      } catch(_) {
        // Fallback: we tonen tenminste de namen
        if (isFolder) folderName.textContent = dt.files.length + ' items gesleept';
        else fileName.textContent = dt.files.length + ' bestanden gesleept';
      }
    });
  }
  wireDrop(dropFiles, fileInput, false);
  wireDrop(dropFolder, folderInput, true);

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
  const useFolder = mode==='folder' && supportsFolderPicker;
  const files = Array.from(useFolder ? folderInput.files : fileInput.files);
  if(!files.length){
    if(useFolder && isAndroid){
      alert("Geen bestanden gekozen. Op deze Android-browser lijkt mapselectie niet te werken. "
          + "Schakel terug naar 'Bestand(en)' en selecteer daar alle bestanden tegelijk.");
    } else {
      alert("Kies eerst "+(useFolder?"een map":"bestanden")+".");
    }
    return;
  }
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
        it.ui.row.classList.add('active');
        it.ui.eta.textContent='Bezig…';
        it.start=performance.now(); log("Start: "+it.rel);
        try{
          const init=await putInit(token,it.f.name,it.f.type);
          let last=0;
          await putWithProgress(init.url,it.f,(loaded,total)=>{
            const pct=Math.round(loaded/total*100);
            const d=loaded-last; last=loaded; moved+=d; it.uploaded=loaded;
            const spent=(performance.now()-it.start)/1000; const sp = loaded/Math.max(spent,0.001);
            const left=total-loaded; const etaS= sp>1 ? left/sp : 0;
            it.ui.eta.textContent = pct + '%' + (etaS ? ' · ' + new Date(etaS*1000).toISOString().substring(14,19) : '');
            setTotal(moved/totBytes*100,'Uploaden…');
          });
          await putComplete(token,init.key,it.f.name,it.rel);
          it.ui.row.classList.remove('active');
          it.ui.row.classList.add('done');
          it.ui.eta.textContent='Klaar';
          done++; log("Klaar: "+it.rel);
        }catch(err){
          it.ui.row.classList.remove('active');
          it.ui.row.classList.add('err');
          it.ui.eta.textContent='Fout';
          log("Fout: "+it.rel);
        }
      }
    } finally { workers--; }
  }
  await Promise.all(Array.from({length:Math.min(FILE_PAR,list.length)}, worker));
  setTotal(100,'Klaar');
  document.getElementById('totalBar').classList.remove('active');

  const link="{{ url_for('package_page', token='__T__', _external=True) }}".replace("__T__", token);
  resBox.innerHTML = `<div class="oh-share">
    <div class="oh-share-head">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
      Upload voltooid · je deelbare link is klaar
    </div>
    <div class="oh-share-row">
      <input id="shareLinkInput" class="oh-input" value="${link}" readonly>
      <button id="copyBtn" type="button" class="oh-btn accent">Kopieer link</button>
    </div>
  </div>`;

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
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Download · {{ title or brand.name }}</title>
  {{ head_icon|safe }}
  <style>
    {{ base_css }}

    :root{
      /* Achtergrond-tinten worden nu overgelaten aan BASE_CSS (aurora).
         Deze custom vars blijven voor de LOCALE component-styling. */
      --oh-surface: rgba(255,255,255,.82); --oh-surface-2: rgba(248,250,252,.70);
      --oh-border: rgba(148,163,184,.35); --oh-border-strong: rgba(148,163,184,.55);
      --oh-text:#0f172a; --oh-muted:#475569;
      --oh-brand:#0f3a6b; --oh-brand-2:#1e5a9e;
      --oh-accent:#d97706; --oh-accent-2:#f59e0b;
      --oh-success:#16a34a; --oh-danger:#dc2626;
      --oh-radius:10px; --oh-radius-sm:6px;
      --oh-shadow:0 1px 3px rgba(15,23,42,.06), 0 18px 40px rgba(15,23,42,.10);
    }
    @media (prefers-color-scheme: dark){
      :root{
        --oh-surface: rgba(17,24,39,.70);
        --oh-surface-2: rgba(15,23,42,.55);
        --oh-border: rgba(148,163,184,.22);
        --oh-border-strong: rgba(148,163,184,.38);
        --oh-text:#e5e7eb; --oh-muted:#9aa3b2;
        --oh-brand:#7db4ff; --oh-brand-2:#4a7fff;
        --oh-shadow: 0 1px 3px rgba(0,0,0,.35), 0 18px 40px rgba(0,0,0,.45);
      }
    }
    *,*::before,*::after{box-sizing:border-box}
    /* Body-achtergrond transparant zodat de aurora (BASE_CSS .bg) doorheen schijnt. */
    html,body{min-height:100%;background:transparent;color:var(--oh-text);
      font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",Roboto,sans-serif;
      font-size:15px;line-height:1.5;margin:0;-webkit-font-smoothing:antialiased}
    .oh-shell{position:relative;z-index:1;max-width:1000px;margin:0 auto;padding:28px 22px 60px}

    /* Top bar */
    .oh-topbar{display:flex;justify-content:space-between;align-items:center;gap:20px;
      padding:14px 20px;background:var(--oh-surface);
      backdrop-filter:blur(10px) saturate(1.05);
      -webkit-backdrop-filter:blur(10px) saturate(1.05);
      border:1px solid var(--oh-border);
      border-radius:var(--oh-radius);box-shadow:var(--oh-shadow);margin-bottom:22px;flex-wrap:wrap}
    .oh-brand{display:flex;align-items:center;gap:12px}
    .oh-brand-mark{width:38px;height:38px;border-radius:8px;
      background:linear-gradient(135deg,var(--oh-brand),var(--oh-brand-2));
      display:grid;place-items:center;color:#fff;font-weight:700;font-size:14px;
      box-shadow:inset 0 -2px 0 rgba(0,0,0,.15);overflow:hidden}
    .oh-brand-mark svg{width:100%;height:100%;display:block}
    .oh-brand-text h1{margin:0;font-size:17px;font-weight:600;color:var(--oh-text)}
    .oh-brand-text p{margin:0;font-size:12px;color:var(--oh-muted)}

    /* Layout */
    .oh-deck{display:grid;grid-template-columns:minmax(0,2fr) minmax(0,1fr);gap:22px}
    @media (max-width:880px){.oh-deck{grid-template-columns:1fr}}

    /* Card */
    .oh-card{background:var(--oh-surface);
      backdrop-filter:blur(10px) saturate(1.05);
      -webkit-backdrop-filter:blur(10px) saturate(1.05);
      border:1px solid var(--oh-border);
      border-radius:var(--oh-radius);box-shadow:var(--oh-shadow);overflow:hidden}
    .oh-card-head{padding:18px 22px;border-bottom:1px solid var(--oh-border);
      display:flex;justify-content:space-between;align-items:center;gap:16px;
      background:var(--oh-surface-2)}
    .oh-card-head h2{margin:0;font-size:15px;font-weight:600;
      display:flex;align-items:center;gap:10px}
    .oh-card-head h2 svg{color:var(--oh-brand)}
    .oh-card-head .meta{font-size:13px;color:var(--oh-muted)}
    .oh-card-head .meta .pill{display:inline-block;padding:2px 8px;background:var(--oh-surface);
      border:1px solid var(--oh-border);border-radius:999px;font-weight:600;color:var(--oh-text)}
    .oh-card-body{padding:22px}

    /* Pakket-info blok */
    .pkg-info{display:flex;flex-wrap:wrap;gap:14px 32px;
      padding:14px 18px;background:var(--oh-surface-2);border:1px solid var(--oh-border);
      border-radius:var(--oh-radius-sm);margin-bottom:18px}
    .pkg-info div .k{font-size:11px;font-weight:600;text-transform:uppercase;
      letter-spacing:.05em;color:var(--oh-muted);margin-bottom:2px}
    .pkg-info div .v{font-size:14px;font-weight:600;color:var(--oh-text);font-variant-numeric:tabular-nums}

    /* Download button */
    .oh-btn{display:inline-flex;align-items:center;gap:8px;padding:12px 22px;
      background:var(--oh-brand);color:#fff;border:none;border-radius:var(--oh-radius-sm);
      font:inherit;font-size:15px;font-weight:600;cursor:pointer;
      transition:background .15s,transform .05s;text-decoration:none}
    .oh-btn:hover:not(:disabled){background:var(--oh-brand-2)}
    .oh-btn:active:not(:disabled){transform:translateY(1px)}
    .oh-btn:disabled{opacity:.5;cursor:not-allowed}
    .oh-btn.accent{background:var(--oh-accent)}
    .oh-btn.accent:hover:not(:disabled){background:var(--oh-accent-2)}
    .oh-btn.ghost{background:var(--oh-surface);color:var(--oh-brand);
      border:1px solid var(--oh-border-strong);padding:8px 14px;font-size:13px}
    .oh-btn.ghost:hover{background:var(--oh-surface-2);border-color:var(--oh-brand-2)}

    /* Progress bar */
    .oh-progress{margin-top:14px}
    .oh-progress-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
    .oh-progress-head .k{font-size:12px;font-weight:600;text-transform:uppercase;
      letter-spacing:.04em;color:var(--oh-muted)}
    .oh-progress-head .v{font-size:14px;font-weight:700;color:var(--oh-brand);
      font-variant-numeric:tabular-nums}
    .oh-bar{height:8px;background:var(--oh-surface-2);border:1px solid var(--oh-border);
      border-radius:999px;overflow:hidden}
    .oh-bar > i{display:block;height:100%;width:0%;
      background:linear-gradient(90deg,var(--oh-brand),var(--oh-brand-2));
      transition:width .25s ease;border-radius:999px;position:relative}
    .oh-bar.active > i::after{content:"";position:absolute;inset:0;
      background:linear-gradient(90deg,transparent 0%,rgba(255,255,255,.35) 50%,transparent 100%);
      animation:oh-shine 1.6s linear infinite}
    @keyframes oh-shine{0%{transform:translateX(-100%)}100%{transform:translateX(100%)}}
    .oh-bar.indet > i{width:40%;animation:oh-indet 1.2s linear infinite}
    @keyframes oh-indet{0%{transform:translateX(-100%)}100%{transform:translateX(250%)}}

    /* Post-success invitation: subtle, opt-in by attention only.
       Lichte achtergrond, dunne border, fade-in. Geen schaduwen, geen accentkleuren. */
    .oh-postdl{margin-top:14px;padding:12px 14px;
      background:var(--oh-surface);border:1px solid var(--oh-border);
      border-radius:var(--oh-radius-sm);
      opacity:0;transform:translateY(-4px);
      transition:opacity .35s ease, transform .35s ease}
    .oh-postdl[data-show="1"]{opacity:1;transform:translateY(0)}
    .oh-postdl-line{display:flex;align-items:center;gap:8px;
      font-size:13px;font-weight:600;color:var(--oh-text)}
    .oh-postdl-line svg{color:var(--oh-brand-2);flex:0 0 auto}
    .oh-postdl-cta{margin-top:6px;font-size:13px;color:var(--oh-muted);line-height:1.5}
    .oh-postdl-cta em{font-style:normal;color:var(--oh-text);font-weight:600;
      background:var(--oh-surface-2);padding:1px 6px;border-radius:4px}
    .oh-postdl-cta a{color:var(--oh-brand-2);text-decoration:none;font-weight:600;margin-left:6px}
    .oh-postdl-cta a:hover{text-decoration:underline}

    /* File list */
    .oh-filelist{margin-top:22px}
    .oh-filelist-head{display:flex;align-items:center;justify-content:space-between;
      margin-bottom:10px}
    .oh-filelist-head h3{margin:0;font-size:12px;font-weight:600;text-transform:uppercase;
      letter-spacing:.05em;color:var(--oh-muted)}
    .oh-filelist-count{font-size:12px;color:var(--oh-muted);font-variant-numeric:tabular-nums}

    .oh-file{display:grid;grid-template-columns:auto 1fr auto auto;gap:12px;
      align-items:center;padding:10px 14px;background:var(--oh-surface);
      border:1px solid var(--oh-border);border-radius:var(--oh-radius-sm);
      margin-bottom:6px;transition:border-color .15s}
    .oh-file:hover{border-color:var(--oh-border-strong)}
    .oh-file .ico{color:var(--oh-muted);display:flex;align-items:center}
    .oh-file .name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
      color:var(--oh-text);font-size:14px}
    .oh-file .size{color:var(--oh-muted);font-size:13px;
      font-variant-numeric:tabular-nums;white-space:nowrap}
    .oh-file .action{font-size:13px}
    .oh-file .action a{color:var(--oh-brand-2);text-decoration:none;font-weight:600;
      padding:4px 10px;border-radius:var(--oh-radius-sm);
      border:1px solid var(--oh-border);transition:all .15s}
    .oh-file .action a:hover{background:var(--oh-surface-2);border-color:var(--oh-brand-2)}

    /* Telemetry card */
    .oh-stats{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    .oh-stat{padding:12px 14px;background:var(--oh-surface-2);
      border:1px solid var(--oh-border);border-radius:var(--oh-radius-sm)}
    .oh-stat .k{font-size:11px;font-weight:600;text-transform:uppercase;
      letter-spacing:.05em;color:var(--oh-muted);margin-bottom:4px}
    .oh-stat .v{font-size:16px;font-weight:600;color:var(--oh-text);
      font-variant-numeric:tabular-nums}

    .oh-status{margin-top:8px;font-size:13px;color:var(--oh-muted);min-height:1.2em}

    /* Animerende puntjes bij "Voorbereiden" — geeft visuele feedback dat
       de server bezig is, zelfs als er nog geen bytes binnenkomen. JS zet
       de class .pulsing op #txt en injecteert drie spans; CSS doet de rest. */
    .oh-status .dot{
      display:inline-block;width:.35em;
      opacity:.2;animation:oh-dot-pulse 1.4s ease-in-out infinite;
    }
    .oh-status .dot:nth-child(1){animation-delay:0s}
    .oh-status .dot:nth-child(2){animation-delay:.2s}
    .oh-status .dot:nth-child(3){animation-delay:.4s}
    @keyframes oh-dot-pulse{
      0%, 80%, 100% { opacity:.2 }
      40%           { opacity:1  }
    }

    /* Footer */
    .oh-footer{margin-top:32px;padding-top:18px;border-top:1px solid var(--oh-border);
      text-align:center;font-size:12px;color:var(--oh-muted)}
    .oh-footer a{color:var(--oh-brand-2);text-decoration:none;margin:0 6px}
    .oh-footer a:hover{text-decoration:underline}

    /* Mobile tweaks */
    @media (max-width:640px){
      .oh-file{grid-template-columns:auto 1fr auto}
      .oh-file .action{grid-column:1/-1;justify-self:end}
      .pkg-info{gap:12px 20px}
    }
  </style>
</head>
<body>
{{ bg|safe }}

<div class="oh-shell">

  <header class="oh-topbar">
    <div class="oh-brand">
      <div class="oh-brand-mark"{% if brand.color %} style="background:{{ brand.color }}"{% endif %}>
        {% if brand.logo_svg %}{{ brand.logo_svg|safe }}{% else %}{{ brand.short }}{% endif %}
      </div>
      <div class="oh-brand-text">
        <h1>{{ brand.name }}</h1>
        <p>Je bestanden staan klaar</p>
      </div>
    </div>
  </header>

  <div class="oh-deck">

    <!-- ======== Download card ======== -->
    <section class="oh-card">
      <div class="oh-card-head">
        <h2>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Downloaden
        </h2>
        <span class="meta">Bestanden: <span class="pill">{{ items|length }}</span></span>
      </div>
      <div class="oh-card-body">

        <div class="pkg-info">
          {% if title %}
          <div>
            <div class="k">Onderwerp</div>
            <div class="v">{{ title }}</div>
          </div>
          {% endif %}
          <div>
            <div class="k">Aantal bestanden</div>
            <div class="v">{{ items|length }}</div>
          </div>
          <div>
            <div class="k">Totale grootte</div>
            <div class="v">{{ total_human }}</div>
          </div>
          <div>
            <div class="k">Verloopt op</div>
            <div class="v">{{ expires_human }}</div>
          </div>
        </div>

        {% if items|length == 1 %}
          <button id="btnDownload" class="oh-btn accent">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Download bestand
          </button>
        {% else %}
          <button id="btnDownload" class="oh-btn accent">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            Alles downloaden (ZIP)
          </button>
        {% endif %}

        <div class="oh-progress" id="progressWrap" style="display:none">
          <div class="oh-progress-head">
            <span class="k">Voortgang</span>
            <span class="v" id="pctText">0%</span>
          </div>
          <div id="bar" class="oh-bar"><i></i></div>
          <div class="oh-status" id="txt">Starten…</div>
        </div>

        <!-- Post-success card: subtiele invitatie, verschijnt pas NA de download.
             Geen modal, geen interrupt. Wie het niet ziet, mist niets. -->
        <div class="oh-postdl" id="postdlCard" hidden>
          <div class="oh-postdl-line">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"/></svg>
            <span>Download geslaagd.</span>
          </div>
          <div class="oh-postdl-cta">
            Zelf ook bestanden delen op <em>jouwbedrijf.{{ base_host }}</em>?
            <a href="https://{{ base_host }}/contact?ref=p_postdl" target="_blank" rel="noopener">Bekijk de mogelijkheden →</a>
          </div>
        </div>

        {% if items|length > 1 %}
        <div class="oh-filelist">
          <div class="oh-filelist-head">
            <h3>Inhoud</h3>
            <span class="oh-filelist-count">{{ items|length }} bestanden</span>
          </div>
          {% for it in items %}
          <div class="oh-file">
            <div class="ico">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
            </div>
            <div class="name" title="{{ it['path'] }}">{{ it["path"] }}</div>
            <div class="size">{{ it["size_h"] }}</div>
            <div class="action">
              <a href="{{ url_for('stream_file', token=token, item_id=it['id']) }}">Los</a>
            </div>
          </div>
          {% endfor %}
        </div>
        {% endif %}

      </div>
    </section>

    <!-- ======== Telemetry card ======== -->
    <aside class="oh-card">
      <div class="oh-card-head">
        <h2>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
          Live status
        </h2>
        <span class="meta">Sessie</span>
      </div>
      <div class="oh-card-body">
        <div class="oh-stats">
          <div class="oh-stat">
            <div class="k">Snelheid</div>
            <div class="v" id="tSpeed">0 B/s</div>
          </div>
          <div class="oh-stat">
            <div class="k">Gedownload</div>
            <div class="v" id="tMoved">0 B</div>
          </div>
          <div class="oh-stat">
            <div class="k">Totaal</div>
            <div class="v" id="tTotal">{{ total_human }}</div>
          </div>
          <div class="oh-stat">
            <div class="k">ETA</div>
            <div class="v" id="tEta">—</div>
          </div>
        </div>
      </div>
    </aside>
  </div>

  <footer class="oh-footer">
    {{ brand.footer }}
    <span style="margin:0 6px;color:var(--oh-border-strong)">|</span>
    <a href="{{ url_for('terms_page') }}">Voorwaarden</a>
    <a href="{{ url_for('privacy_page') }}">Privacy</a>
  </footer>
</div>

<script>
const bar=document.getElementById('bar'), fill=bar?bar.querySelector('i'):null;
const txt=document.getElementById('txt'), pctText=document.getElementById('pctText');
const progressWrap=document.getElementById('progressWrap');
const tSpeed=document.getElementById('tSpeed'), tMoved=document.getElementById('tMoved'), tEta=document.getElementById('tEta');

function fmtBytes(n){const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i?n.toFixed(1):Math.round(n))+" "+u[i]}
function showProgress(){ if(progressWrap) progressWrap.style.display='block'; if(bar) bar.classList.add('active'); }
function setPct(p){
  const pct = Math.max(0,Math.min(100,p));
  if(fill) fill.style.width=pct+'%';
  if(pctText) pctText.textContent=Math.round(pct)+'%';
}

// setStatus: gebruik dit ipv txt.textContent=..., zodat we de "pulsing dots"
// animatie netjes aan- en uitzetten. Met pulse=true verschijnt na de tekst
// een rij van drie puntjes die om de beurt opfaden — zo zie je dat er ook
// echt iets gebeurt tijdens "Voorbereiden", als de server nog wacht op B2.
function setStatus(text, pulse){
  if(!txt) return;
  txt.classList.toggle('pulsing', !!pulse);
  if(pulse){
    // Build: "<text> <span.dot>.</span><span.dot>.</span><span.dot>.</span>"
    txt.textContent = text + ' ';
    for(let i=0;i<3;i++){
      const s=document.createElement('span');
      s.className='dot'; s.textContent='.';
      txt.appendChild(s);
    }
  } else {
    txt.textContent = text;
  }
}

// Download-lock: voorkom dat dezelfde knop meerdere parallelle downloads
// start als iemand snel achter elkaar klikt of via toetsenbord-Enter spamt.
// Server-side zou dat een aparte zip-pipeline per klik openen — duur en
// verwarrend voor de gebruiker (drie "Voorbereiden"-states tegelijk).
let _isDownloading = false;

function _setBtnState(disabled, label){
  const b = document.getElementById('btnDownload');
  if(!b) return;
  b.disabled = !!disabled;
  if(label !== undefined){
    // Bewaar het oorspronkelijke label zodat we 'm later kunnen herstellen.
    if(!b.dataset._origHtml){ b.dataset._origHtml = b.innerHTML; }
    if(disabled){
      b.innerHTML = label;
    } else {
      b.innerHTML = b.dataset._origHtml || label;
    }
  }
}

async function downloadWithTelemetry(url, fallbackName){
  // Guard tegen dubbele starts: deze flag is voldoende want alle downloads
  // vanuit deze pagina lopen via deze functie.
  if(_isDownloading) return;
  _isDownloading = true;
  _setBtnState(true, 'Bezig…');

  // Toon meteen de "Voorbereiden..." staat met geanimeerde indeterminate balk.
  // De server kan een paar seconden bezig zijn met prefetch voordat de eerste
  // echte bytes binnenkomen; zonder deze feedback lijkt het alsof er niks gebeurt.
  showProgress();
  if(pctText) pctText.textContent='…';
  if(fill) fill.style.width='0%';
  if(bar){ bar.classList.add('active'); bar.classList.add('indet'); }
  setStatus('Voorbereiden', true);
  if(tSpeed) tSpeed.textContent='—';
  if(tMoved) tMoved.textContent='0 B';
  if(tEta)   tEta.textContent='—';

  let speedAvg=0, lastT=performance.now(), lastB=0, moved=0, total=0;
  let firstChunkSeen=false;

  const tick = ()=>{
    if(!firstChunkSeen) return; // pas tellen als er echt data stroomt
    const now=performance.now(), dt=(now-lastT)/1000; lastT=now;
    const inst=(moved-lastB)/Math.max(dt,0.001); lastB=moved;
    speedAvg = speedAvg? speedAvg*0.7 + inst*0.3 : inst;
    tSpeed.textContent=fmtBytes(speedAvg)+'/s';
    const eta = (total && speedAvg>1) ? (total-moved)/speedAvg : 0;
    tEta.textContent = eta? new Date(eta*1000).toISOString().substring(11,19) : '—';
  };
  const iv=setInterval(tick,700);

  try{
    const res=await fetch(url,{credentials:'same-origin'});
    if(!res.ok){
      setStatus('Fout '+res.status, false);
      clearInterval(iv);
      if(bar){ bar.classList.remove('active'); bar.classList.remove('indet'); }
      return;
    }
    total=parseInt(res.headers.get('Content-Length')||'0',10);
    const name=res.headers.get('X-Filename')||fallbackName||'download';

    const rdr = res.body && res.body.getReader ? res.body.getReader() : null;
    if(rdr){
      const chunks=[];
      while(true){
        const {done,value}=await rdr.read(); if(done) break;
        if(!firstChunkSeen){
          // Eerste byte binnen: wissel van "Voorbereiden..." naar echte voortgang.
          firstChunkSeen=true;
          lastT=performance.now(); lastB=0;
          if(total){
            if(bar) bar.classList.remove('indet');
            setStatus('Downloaden', false);
          } else {
            // Geen Content-Length (zip-stream): houd indet-balk aan, label
            // krijgt wel pulserende dots want we weten niet hoe lang nog.
            setStatus('Downloaden', true);
          }
        }
        chunks.push(value); moved+=value.length;
        if(tMoved) tMoved.textContent=fmtBytes(moved);
        if(total){ setPct(Math.round(moved/total*100)); }
      }
      if(!total){ if(bar) bar.classList.remove('indet'); setPct(100); }
      setStatus('Gereed — bestand wordt opgeslagen', false);
      if(bar) bar.classList.remove('active');
      clearInterval(iv);
      const blob=new Blob(chunks); const u=URL.createObjectURL(blob);
      const a=document.createElement('a'); a.href=u; a.download=name; a.rel='noopener';
      document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(u);
      revealPostDownloadCard();
      return;
    }
    // Geen streaming reader beschikbaar: blijf op indeterminate tot we de blob hebben.
    setStatus('Downloaden', true);
    const blob=await res.blob(); clearInterval(iv);
    if(bar){ bar.classList.remove('indet'); bar.classList.remove('active'); }
    setPct(100); setStatus('Gereed', false);
    const u=URL.createObjectURL(blob); const a=document.createElement('a');
    a.href=u; a.download=fallbackName||'download'; a.click(); URL.revokeObjectURL(u);
    revealPostDownloadCard();
  }catch(e){
    clearInterval(iv);
    if(bar){ bar.classList.remove('active'); bar.classList.remove('indet'); }
    setStatus('Er ging iets mis. Probeer opnieuw.', false);
  }finally{
    // Altijd unlock'en, ook bij fout. Bij succes 1.5s delay zodat "Gereed"
    // leesbaar blijft; bij fout 800ms zodat de foutmelding ook zichtbaar is
    // voor de gebruiker iets opnieuw kan klikken.
    const delay = _txtSaysDone() ? 1500 : 800;
    setTimeout(()=>{ _isDownloading = false; _setBtnState(false); }, delay);
  }
}

function _txtSaysDone(){
  // Helper voor finally-block: kijk of de huidige status-tekst op 'Gereed' staat.
  const t = txt && txt.textContent ? txt.textContent.toLowerCase() : '';
  return t.indexOf('gereed') !== -1;
}

// Toon de post-download invitatie pas NA success — en met een korte delay
// zodat het oog eerst de "Gereed"-tekst opvangt en pas daarna de card opmerkt.
// Geen modal, geen scroll-to, geen pulse: gewoon zacht binnenfaden.
let _postDlShown = false;
function revealPostDownloadCard(){
  if(_postDlShown) return;
  _postDlShown = true;
  const card = document.getElementById('postdlCard');
  if(!card) return;
  setTimeout(()=>{
    card.hidden = false;
    // Force reflow zodat de transition op opacity/transform daadwerkelijk speelt
    void card.offsetWidth;
    card.setAttribute('data-show','1');
  }, 600);
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
<meta name="csrf-token" content="{{ csrf_token() }}"/>
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
  {% set _trial_url = trial_signup_url() %}
  {% if _trial_url %}
  <p class="small" style="margin:.2rem 0 1.2rem 0;color:#475569">
    Liever eerst zonder verplichtingen ervaren hoe het werkt?
    <a href="{{ _trial_url }}">Probeer de gratis variant →</a>
  </p>
  {% endif %}
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}

  <form method="post" action="{{ url_for('contact') }}" novalidate id="contactForm">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <input type="hidden" name="ref" id="contactRef" value="">
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

    <button class="btn-pro primary" type="submit">Verstuur aanvraag</button>

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

// ---------- bron-tracking ----------
// Vul het hidden ref-veld vanuit ?ref=... in de URL. Whitelist op alphanumeric,
// max 32 chars (server filtert nogmaals, maar client-side scheelt rondreis).
(function(){
  try{
    const refEl = document.getElementById('contactRef');
    if(!refEl) return;
    const params = new URLSearchParams(window.location.search);
    const raw = (params.get('ref') || '').slice(0, 32);
    const clean = raw.replace(/[^a-zA-Z0-9_\-]/g, '');
    if(clean){ refEl.value = clean; }
  }catch(e){ /* niet kritiek */ }
})();

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
        const csrf = (document.querySelector('meta[name="csrf-token"]')||{}).content || '';
        const emailEl = document.getElementById('login_email');
        await fetch("{{ url_for('paypal_store_subscription') }}", {
          method: "POST",
          headers: {"Content-Type":"application/json","X-CSRF-Token":csrf},
          body: JSON.stringify({
            subscription_id: data.subscriptionID,
            plan_value: (document.getElementById('storage_tb')?.value || ""),
            login_email: (emailEl ? emailEl.value.trim() : "")
          })
        });
        alert("Bedankt! Je abonnement is gestart. Zodra de betaling is bevestigd, wordt je account automatisch aangemaakt en ontvang je een e-mail. ID: " + data.subscriptionID);
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
<p class="footer">{{ brand.footer }}</p></div></div>
</body></html>
"""

CONTACT_MAIL_FALLBACK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title>{{ head_icon|safe }}<style>{{ base_css }}</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card">
  <h1>Aanvraag gereed</h1>
  <p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p>
  <a class="btn-pro primary" href="{{ mailto_link }}">Open e-mail</a>
  <p class="footer">{{ brand.footer }}</p>
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

# EXPIRED_HTML: geëxtraheerd naar templates/expired.html voor snellere Python-parsing.
# Wordt eenmalig bij app-startup ingelezen (geen disk I/O per request).
_EXPIRED_TEMPLATE_PATH = BASE_DIR / "templates" / "expired.html"
try:
    EXPIRED_HTML = _EXPIRED_TEMPLATE_PATH.read_text(encoding="utf-8")
except FileNotFoundError:
    raise RuntimeError(
        f"❌ Template ontbreekt: {_EXPIRED_TEMPLATE_PATH}. "
        "Zorg dat het bestand meegedeployed wordt naast app_claude.py."
    )




PRIVACY_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Privacyverklaring – downloadlink.nl</title>{{ head_icon|safe }}
<style>
{{ base_css }}
h1{color:var(--brand);margin:.2rem 0 1rem}
h2{margin:1.2rem 0 .4rem}
.section{margin-bottom:1.1rem}
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

# -------- CSRF bescherming --------
# Alle state-changing verzoeken (POST/PUT/PATCH/DELETE) moeten een token meesturen.
# HTML-forms: <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
# JSON fetch: header 'X-CSRF-Token: <token>' (in HTML via <meta name="csrf-token" ...>).
# Uitgezonderd: webhooks (externe callers), de interne cleanup-route (gescheiden auth)
# en de publieke leaderboard-submit (eigen IP-rate-limit).
CSRF_EXEMPT_PREFIXES = ("/webhook/", "/internal/", "/health", "/__health", "/api/leaderboard")

def _get_csrf_token() -> str:
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["_csrf"] = tok
    return tok

@app.context_processor
def _inject_csrf():
    """Maakt csrf_token(), trial_signup_url(), brand en is_trial beschikbaar in alle Jinja-templates."""
    try:
        brand = current_brand()
        is_trial = is_trial_tenant()
    except Exception:
        # Buiten request-context (zoals bij errors voor request setup) of bij
        # onbekende tenant: lege defaults zodat templates niet crashen.
        brand = {"name": "Bestandentransfer", "short": "FT",
                 "tagline": "Beveiligde bestandsoverdracht",
                 "footer": "Bestandentransfer", "logo_svg": "", "color": ""}
        is_trial = False
    return {
        "csrf_token": _get_csrf_token,
        "trial_signup_url": trial_signup_url,
        "brand": brand,
        "is_trial": is_trial,
    }

@app.before_request
def _csrf_protect():
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    path = request.path or ""
    if any(path.startswith(p) for p in CSRF_EXEMPT_PREFIXES):
        return
    expected = session.get("_csrf") or ""
    submitted = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("_csrf")
        or ""
    )
    # Voor JSON-endpoints die via fetch() worden aangeroepen maar waar de body
    # niet eerst door get_json() hoeft: check ook of token in JSON body zit.
    if not submitted and request.is_json:
        try:
            body = request.get_json(silent=True) or {}
            submitted = body.get("_csrf") or ""
        except Exception:
            submitted = ""
    if not expected or not submitted or not hmac.compare_digest(expected, submitted):
        log.warning("CSRF rejected: path=%s ip=%s", path, (request.remote_addr or ""))
        abort(400, description="CSRF token missing or invalid")

@app.after_request
def apply_default_headers(resp):
    resp.headers.setdefault("X-Request-ID", getattr(g, "request_id", "-"))
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    # HSTS: dwingt browsers af om HTTPS te gebruiken (na het eerste bezoek).
    # 1 jaar, includeSubDomains. Geen 'preload' totdat je je domein bij
    # hstspreload.org hebt aangemeld — preload is permanent en moeilijk terug te draaien.
    resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    # Content Security Policy: strict-ish maar ruimte voor PayPal SDK, Three.js
    # (voor het arcade-spel op de EXPIRED_HTML pagina), en inline styles/scripts.
    # Pas aan als je externe hosts toevoegt.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline' "
            "https://www.paypal.com https://www.paypalobjects.com "
            "https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "frame-src 'self' https://www.paypal.com; "
        "connect-src 'self' https://*.paypal.com "
            "https://*.backblazeb2.com https://*.s3.eu-central-003.backblazeb2.com; "
        "worker-src 'self' blob:"
    )
    if request.path.startswith(("/login", "/logout")):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp

# -------------- Helpers --------------
def logged_in() -> bool:
    return bool(session.get("authed") and session.get("user_id"))

def current_user():
    """Haal de huidige user op (uit DB), of None. Gecached per request op g."""
    cached = getattr(g, "_cached_user", "__unset__")
    if cached != "__unset__":
        return cached
    uid = session.get("user_id")
    if not uid:
        g._cached_user = None
        return None
    conn = db()
    try:
        row = conn.execute(
            "SELECT id, email, is_admin, tenant_id, disabled, is_trial, email_verified FROM users WHERE id = ?",
            (uid,)
        ).fetchone()
        if row is None or row["disabled"]:
            g._cached_user = None
            return None
        g._cached_user = row
        return row
    finally:
        conn.close()

def current_user_id():
    u = current_user()
    return u["id"] if u else None

def is_admin() -> bool:
    u = current_user()
    return bool(u and u["is_admin"])

def human(n: int) -> str:
    try:
        x = float(n)
    except (TypeError, ValueError):
        x = 0.0
    if x < 0:
        x = 0.0
    for u in ["B","KB","MB","GB","TB"]:
        if x < 1024 or u == "TB":
            return f"{x:.1f} {u}" if u!="B" else f"{int(x)} {u}"
        x /= 1024
    return f"{x:.1f} TB"

def send_email(to_addr: str, subject: str, body: str, html: str | None = None):
    """Stuur een e-mail. Als 'html' is meegegeven, wordt een multipart-bericht
    verstuurd met zowel een plain-text versie (body) als een HTML-versie.
    Mailclients tonen dan de HTML-versie met klikbare links; oudere clients
    vallen automatisch terug op plain-text.
    """
    if not to_addr or not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        log.warning("E-mail niet verstuurd: SMTP niet (volledig) geconfigureerd")
        return
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_addr
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)

def send_email_async(to_addr: str, subject: str, body: str, html: str | None = None):
    """Verstuur e-mail op een achtergrondthread.

    Belangrijk voor PayPal-webhooks: anders blokkeert SMTP de webhook-respons
    (timeout 20s); PayPal retried dan en je krijgt dubbele mails. Voor
    contactformulieren scheelt het zichtbaar de "verzenden..." wachttijd.
    """
    def _run():
        try:
            send_email(to_addr, subject, body, html=html)
        except Exception:
            log.exception("async send_email failed (to=%s, subject=%s)", to_addr, subject)
    try:
        ex = globals().get("_bg_executor")
        if ex is not None:
            ex.submit(_run)
            return
    except Exception:
        log.exception("send_email_async submit failed")
    # Fallback: synchroon
    _run()

_paypal_token_cache = {"token": None, "exp": 0.0}
_paypal_token_lock = threading.Lock()

def paypal_access_token():
    if not PAYPAL_CLIENT_ID or not PAYPAL_CLIENT_SECRET:
        raise RuntimeError("PAYPAL_CLIENT_ID/SECRET ontbreekt")
    now = time.time()
    # Snelle pad zonder lock als cache nog geldig is
    if _paypal_token_cache["token"] and _paypal_token_cache["exp"] > now + 60:
        return _paypal_token_cache["token"]
    with _paypal_token_lock:
        # Double-check binnen de lock (andere thread kan al ververst hebben)
        now = time.time()
        if _paypal_token_cache["token"] and _paypal_token_cache["exp"] > now + 60:
            return _paypal_token_cache["token"]
        req = urllib.request.Request(PAYPAL_API_BASE + "/v1/oauth2/token", method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        creds = f"{PAYPAL_CLIENT_ID}:{PAYPAL_CLIENT_SECRET}".encode()
        req.add_header("Authorization", "Basic " + base64.b64encode(creds).decode())
        data = "grant_type=client_credentials".encode()
        with urllib.request.urlopen(req, data=data, timeout=20) as resp:
            j = json.loads(resp.read().decode())
            tok = j["access_token"]
            expires_in = int(j.get("expires_in", 32000))
            _paypal_token_cache["token"] = tok
            _paypal_token_cache["exp"] = now + max(60, expires_in - 60)
            return tok

# --------- Basishost voor subdomein-preview ----------
def get_base_host():
    # Bepaal basisdomein voor voorbeeldlinks. Configureerbaar via BASE_HOST env.
    # Fallback: strip de subdomein-prefix van de canonical host.
    explicit = os.environ.get("BASE_HOST", "").strip().lower()
    if explicit:
        return explicit
    host = CANONICAL_HOST
    parts = host.split(".")
    if len(parts) > 2:
        return ".".join(parts[-2:])
    return host


# -------------- Routes (core) --------------
# Opgeschoond: dubbele routeblokken verwijderd en configuratie iets robuuster gemaakt.

@app.route("/debug/dbcols")
def debug_dbcols():
    if not logged_in():
        abort(404)
    if not app.debug and os.environ.get("ENABLE_DEBUG_ROUTES") != "1":
        abort(404)
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
    return render_template_string(INDEX_HTML, user=session.get("user"), is_admin=is_admin(), base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON)

# -------- Rate limiting (brute-force bescherming) --------
# Backend: SQLite-tabel rate_limits. Werkt over meerdere gunicorn-workers heen
# en lekt niet geheugen. Scope-veld ondersteunt aparte buckets (login, pkgview, ...).
LOGIN_MAX_ATTEMPTS = int(os.environ.get("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_WINDOW_SECONDS = int(os.environ.get("LOGIN_WINDOW_SECONDS", "300"))        # 5 minuten
LOGIN_LOCKOUT_SECONDS = int(os.environ.get("LOGIN_LOCKOUT_SECONDS", "900"))      # 15 minuten

PKGVIEW_MAX_ATTEMPTS = int(os.environ.get("PKGVIEW_MAX_ATTEMPTS", "30"))
PKGVIEW_WINDOW_SECONDS = int(os.environ.get("PKGVIEW_WINDOW_SECONDS", "60"))     # 1 minuut
PKGVIEW_LOCKOUT_SECONDS = int(os.environ.get("PKGVIEW_LOCKOUT_SECONDS", "300"))  # 5 minuten

def _client_ip() -> str:
    """Client-IP via ProxyFix (request.remote_addr is al gecorrigeerd)."""
    return (request.remote_addr or "")[:64]

def _login_client_ip() -> str:
    """Backwards-compatible alias."""
    return _client_ip()

def _rate_is_blocked(scope: str, ip: str) -> float:
    """Return seconds tot unblock, of 0 als niet geblokkeerd."""
    if not ip:
        return 0.0
    conn = db()
    try:
        row = conn.execute(
            "SELECT blocked_until FROM rate_limits WHERE scope = ? AND ip = ?",
            (scope, ip)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return 0.0
    now = time.time()
    bu = float(row["blocked_until"] or 0)
    if bu and now < bu:
        return bu - now
    return 0.0

def _rate_register_failure(scope: str, ip: str, max_attempts: int, window: int, lockout: int) -> None:
    if not ip:
        return
    now = time.time()
    conn = db()
    try:
        row = conn.execute(
            "SELECT count, first_ts, blocked_until FROM rate_limits WHERE scope = ? AND ip = ?",
            (scope, ip)
        ).fetchone()
        if row is None or (now - float(row["first_ts"] or 0)) > window:
            conn.execute(
                "INSERT OR REPLACE INTO rate_limits(scope, ip, count, first_ts, blocked_until) VALUES(?,?,?,?,?)",
                (scope, ip, 1, now, 0.0)
            )
        else:
            count = int(row["count"]) + 1
            blocked_until = now + lockout if count >= max_attempts else 0.0
            conn.execute(
                "UPDATE rate_limits SET count = ?, blocked_until = ? WHERE scope = ? AND ip = ?",
                (count, blocked_until, scope, ip)
            )
        conn.commit()
    finally:
        conn.close()

def _rate_reset(scope: str, ip: str) -> None:
    if not ip:
        return
    conn = db()
    try:
        conn.execute("DELETE FROM rate_limits WHERE scope = ? AND ip = ?", (scope, ip))
        conn.commit()
    finally:
        conn.close()

def _rate_cleanup_periodic() -> None:
    """Ruim oude rate-limit records op. Wordt periodiek door healthcheck aangeroepen."""
    cutoff = time.time() - max(LOGIN_LOCKOUT_SECONDS, PKGVIEW_LOCKOUT_SECONDS) - 3600
    conn = db()
    try:
        conn.execute(
            "DELETE FROM rate_limits WHERE blocked_until < ? AND first_ts < ?",
            (time.time(), cutoff)
        )
        conn.commit()
    finally:
        conn.close()

# Specifieke login-wrappers (backwards compat met bestaande code)
def _login_is_blocked(ip: str) -> float:
    return _rate_is_blocked("login", ip)

def _login_register_failure(ip: str) -> None:
    _rate_register_failure("login", ip, LOGIN_MAX_ATTEMPTS, LOGIN_WINDOW_SECONDS, LOGIN_LOCKOUT_SECONDS)

def _login_reset(ip: str) -> None:
    _rate_reset("login", ip)

def _verify_password(stored_hash: str, submitted: str) -> bool:
    """Controleer wachtwoord tegen stored hash. Constant-time."""
    if not stored_hash:
        return False
    try:
        return check_password_hash(stored_hash, submitted)
    except Exception:
        return False

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        ip = _login_client_ip()
        wait = _login_is_blocked(ip)
        if wait > 0:
            return render_template_string(
                LOGIN_HTML,
                error=f"Te veel mislukte pogingen. Probeer het over {int(wait//60)+1} minuten opnieuw.",
                base_css=BASE_CSS, bg=BG_DIV,
                auth_email="",
                head_icon=HTML_HEAD_ICON
            ), 429

        email = (request.form.get("email") or "").lower().strip()
        pw    = (request.form.get("password") or "").strip()

        # Zoek user in DB
        conn = db()
        try:
            row = conn.execute(
                "SELECT id, email, password_hash, is_admin, tenant_id, disabled, is_trial, email_verified FROM users WHERE email = ?",
                (email,)
            ).fetchone()
        finally:
            conn.close()

        # Altijd een dummy-hash checken als user niet bestaat, om timing te normaliseren
        pw_ok = False
        if row is not None and not row["disabled"]:
            pw_ok = _verify_password(row["password_hash"], pw)
        else:
            # Dummy compute zodat response-tijd niet verraadt of user bestaat
            check_password_hash("pbkdf2:sha256:600000$x$" + "0"*64, pw)

        if row is not None and not row["disabled"] and pw_ok:
            # Onverifieerde accounts (zoals trial vóór e-mail-bevestiging) mogen
            # niet inloggen. Geen apart pad zodat e-mail-enumeratie niet mogelijk is.
            if not row["email_verified"]:
                _login_register_failure(ip)
                time.sleep(0.3)
                return render_template_string(
                    LOGIN_HTML,
                    error="Bevestig eerst je e-mailadres via de link die we je hebben gestuurd.",
                    base_css=BASE_CSS, bg=BG_DIV,
                    auth_email="",
                    head_icon=HTML_HEAD_ICON
                )
            _login_reset(ip)
            session.clear()
            session.permanent = True
            session["authed"] = True
            session["user_id"] = row["id"]
            session["user"] = row["email"]
            session["is_admin"] = bool(row["is_admin"])
            return redirect(url_for("index"))

        _login_register_failure(ip)
        time.sleep(0.3)
        return render_template_string(
            LOGIN_HTML,
            error="Onjuiste inloggegevens.",
            base_css=BASE_CSS, bg=BG_DIV,
            auth_email="",
            head_icon=HTML_HEAD_ICON
        )

    return render_template_string(
        LOGIN_HTML,
        error=None,
        base_css=BASE_CSS, bg=BG_DIV,
        auth_email="",
        head_icon=HTML_HEAD_ICON
    )

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ============================================================
# TRIAL (gratis variant) — signup, e-mail-verificatie, restricties
# ============================================================

# Wegwerp-mailproviders die we niet accepteren voor trial-signups.
# Een korte lijst is genoeg — dekt 90% van de gevallen, en kost geen externe
# afhankelijkheid. Uitbreidbaar via env var TRIAL_DISPOSABLE_DOMAINS (komma-gescheiden).
_DISPOSABLE_DOMAINS_BUILTIN = {
    "mailinator.com", "guerrillamail.com", "guerrillamail.info", "guerrillamail.biz",
    "10minutemail.com", "10minutemail.net", "20minutemail.com", "30minutemail.com",
    "tempmail.com", "temp-mail.org", "tempmailo.com", "tempmail.net", "tmpmail.org",
    "yopmail.com", "trashmail.com", "trashmail.de", "throwawaymail.com",
    "maildrop.cc", "sharklasers.com", "getnada.com", "getairmail.com",
    "dispostable.com", "fakeinbox.com", "mintemail.com", "mailnesia.com",
    "spambox.us", "spamgourmet.com", "spam4.me", "mohmal.com",
    "emailondeck.com", "tempinbox.com", "tempmailaddress.com",
}
_disposable_extra = os.environ.get("TRIAL_DISPOSABLE_DOMAINS", "").lower().strip()
DISPOSABLE_DOMAINS = _DISPOSABLE_DOMAINS_BUILTIN | {
    d.strip() for d in _disposable_extra.split(",") if d.strip()
}

# E-mail format-check: simpel maar adequaat. Voor trial-signup is een snelle
# format-check + DNS-checks via de SMTP-server zelf voldoende.
_EMAIL_RE_TRIAL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Rate-limit scopes (sluit aan op bestaand rate_limits-systeem)
TRIAL_SIGNUP_MAX_PER_DAY = int(os.environ.get("TRIAL_SIGNUP_MAX_PER_DAY", "1"))
TRIAL_SIGNUP_WINDOW = 24 * 3600
TRIAL_SIGNUP_LOCKOUT = 24 * 3600

def _is_disposable_email(email: str) -> bool:
    parts = (email or "").lower().rsplit("@", 1)
    if len(parts) != 2:
        return True
    return parts[1] in DISPOSABLE_DOMAINS

def _email_already_in_use(email: str) -> bool:
    """Bestaat er al een (verified) user met dit e-mailadres?"""
    conn = db()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE email = ? LIMIT 1", (email,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()

def _enforce_trial_size_limit(byte_size: int) -> tuple:
    """Return (ok: bool, error_message: str)."""
    if byte_size > TRIAL_MAX_BYTES_PER_PACKAGE:
        gb = TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)
        return False, f"In de gratis variant is het maximum {gb:.0f} GB per pakket. Upgrade voor grotere uploads."
    return True, ""

def _enforce_trial_active_count(conn, user_id: int) -> tuple:
    """Hoeveel niet-verlopen pakketten heeft deze trial-gebruiker al?"""
    now_iso = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM packages WHERE owner_user_id = ? AND expires_at > ?",
        (user_id, now_iso)
    ).fetchone()
    n = int(row["n"] or 0)
    if n >= TRIAL_MAX_ACTIVE_PACKAGES:
        return False, f"In de gratis variant kun je maximaal {TRIAL_MAX_ACTIVE_PACKAGES} actieve pakketten hebben. Verwijder een ouder pakket of upgrade."
    return True, ""

def _enforce_trial_ttl(days: float) -> float:
    """Cap TTL op trial-max. Anders unchanged."""
    return min(float(days), TRIAL_MAX_TTL_DAYS)

def _send_verify_email(to_addr: str, verify_url: str):
    """Stuurt de verificatiemail asynchroon (multipart: plain-text + HTML)."""
    # Bepaal de host voor links in de footer (voorwaarden, privacy, home).
    # Gebruikt het canonieke domein van deze server (oldehanter.downloadlink.nl
    # of downloadlink.nl, afhankelijk van waar de signup vandaan kwam).
    site_host = _trial_host or _tenant_host
    site_url = f"https://{site_host}"
    terms_url = f"{site_url}/terms"
    privacy_url = f"{site_url}/privacy"

    subject = "Bevestig je e-mailadres voor downloadlink.nl"

    # Plain-text fallback (voor mailclients zonder HTML-ondersteuning).
    body = (
        "Hallo,\n\n"
        "Bedankt voor je aanmelding bij downloadlink.nl. Klik op de onderstaande "
        "link om je e-mailadres te bevestigen en je gratis account te activeren:\n\n"
        f"{verify_url}\n\n"
        f"Deze link is {TRIAL_VERIFY_TOKEN_TTL_HRS} uur geldig.\n\n"
        "Heb je je niet aangemeld? Negeer deze e-mail dan; er gebeurt verder niets.\n\n"
        "—\n"
        "downloadlink.nl • Bestandentransfer\n"
        f"{site_url}\n"
        f"Algemene voorwaarden: {terms_url}\n"
        f"Privacyverklaring:    {privacy_url}\n"
    )

    # HTML-variant. Tabel-layout en inline styles want externe CSS wordt door
    # veel mailclients (Outlook, Gmail) genegeerd of gestript. Compatibel met
    # Outlook 2007-2024, Gmail, Apple Mail, Teams, mobiel.
    html = f"""\
<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bevestig je e-mailadres</title>
</head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#0f172a">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f1f5f9">
    <tr><td align="center" style="padding:32px 16px">

      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="max-width:560px;background:#ffffff;border-radius:14px;overflow:hidden;border:1px solid #e2e8f0">

        <tr><td style="background:#0f4c98;padding:22px 28px">
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
            <tr>
              <td style="font-size:20px;font-weight:700;color:#ffffff;letter-spacing:.2px">
                downloadlink<span style="color:#9ec5ff">.nl</span>
              </td>
              <td align="right" style="font-size:12px;color:#cfe0fa;text-transform:uppercase;letter-spacing:1px">
                Bestandentransfer
              </td>
            </tr>
          </table>
        </td></tr>

        <tr><td style="padding:32px 28px 8px 28px">
          <h1 style="margin:0 0 14px 0;font-size:22px;font-weight:600;color:#0f172a;line-height:1.3">
            Bevestig je e-mailadres
          </h1>
          <p style="margin:0 0 12px 0;font-size:15px;line-height:1.6;color:#334155">
            Bedankt voor je aanmelding bij <strong>downloadlink.nl</strong>.
            Klik op de knop hieronder om je e-mailadres te bevestigen en je
            gratis account te activeren.
          </p>
        </td></tr>

        <tr><td style="padding:18px 28px 8px 28px" align="left">
          <table role="presentation" cellpadding="0" cellspacing="0" border="0">
            <tr><td style="background:#1c62d2;border-radius:10px">
              <a href="{verify_url}"
                 style="display:inline-block;padding:13px 26px;font-size:15px;font-weight:600;
                        color:#ffffff;text-decoration:none;border-radius:10px;
                        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif">
                Bevestig e-mailadres
              </a>
            </td></tr>
          </table>
        </td></tr>

        <tr><td style="padding:14px 28px 4px 28px">
          <p style="margin:14px 0 6px 0;font-size:13px;color:#64748b;line-height:1.5">
            Werkt de knop niet? Kopieer dan deze link naar je browser:
          </p>
          <p style="margin:0 0 12px 0;font-size:13px;word-break:break-all;line-height:1.5">
            <a href="{verify_url}" style="color:#0f4c98;text-decoration:underline">{verify_url}</a>
          </p>
          <p style="margin:0 0 4px 0;font-size:13px;color:#64748b;line-height:1.5">
            Deze link is <strong>{TRIAL_VERIFY_TOKEN_TTL_HRS} uur</strong> geldig.
          </p>
        </td></tr>

        <tr><td style="padding:18px 28px 24px 28px;border-top:1px solid #e2e8f0">
          <p style="margin:14px 0 0 0;font-size:12px;color:#94a3b8;line-height:1.5">
            Heb je je niet aangemeld? Negeer deze e-mail dan; er gebeurt
            verder niets en het account wordt niet geactiveerd.
          </p>
        </td></tr>

        <tr><td style="padding:16px 28px 22px 28px;background:#f8fafc;border-top:1px solid #e2e8f0">
          <p style="margin:0 0 6px 0;font-size:12px;color:#475569;line-height:1.5">
            <strong style="color:#0f172a">downloadlink.nl</strong> &middot; Bestandentransfer
          </p>
          <p style="margin:0;font-size:12px;color:#64748b;line-height:1.6">
            <a href="{site_url}" style="color:#475569;text-decoration:underline">Website</a>
            &nbsp;&middot;&nbsp;
            <a href="{terms_url}" style="color:#475569;text-decoration:underline">Algemene voorwaarden</a>
            &nbsp;&middot;&nbsp;
            <a href="{privacy_url}" style="color:#475569;text-decoration:underline">Privacyverklaring</a>
          </p>
          <p style="margin:10px 0 0 0;font-size:11px;color:#94a3b8;line-height:1.5">
            Je ontvangt deze e-mail omdat dit e-mailadres is gebruikt om een
            account aan te maken op {site_host}. Reageer niet op dit bericht.
          </p>
        </td></tr>

      </table>

    </td></tr>
  </table>
</body></html>
"""
    send_email_async(to_addr, subject, body, html=html)

# -------------------- Trial signup form --------------------

TRIAL_SIGNUP_HTML = r"""
<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{{ head_icon|safe }}<title>Gratis variant – aanmelden</title>
<style>
  {{ base_css|safe }}

  /* =====================================================
     TRIAL SIGNUP — solid card, identiek aan contactpagina
     ===================================================== */
  .card.contact-card{
    background: rgba(255,255,255,0.9) !important;
    color: #0f172a !important;
    border: 1px solid rgba(0,0,0,0.08) !important;
    border-radius: 20px;
    box-shadow: 0 18px 40px rgba(0,0,0,.18);
    backdrop-filter: blur(12px) saturate(1.2);
  }
  .card.contact-card h1,
  .card.contact-card label{ color:#0f172a !important; }
  .card.contact-card .small,
  .card.contact-card .muted{ color:#334155 !important; }

  .card.contact-card .input,
  .card.contact-card input[type=email],
  .card.contact-card input[type=password]{
    color:#0f172a !important;
    background:#f8fafc !important;
    border:1px solid #cbd5e1 !important;
  }
  .card.contact-card input::placeholder{ color:#6b7280 !important; }
  .card.contact-card .input:focus,
  .card.contact-card input:focus{
    border-color:#2563eb !important;
    box-shadow:0 0 0 4px color-mix(in oklab, #2563eb 25%, transparent) !important;
    outline:0;
  }

  .card.contact-card a{ color:#0f4c98 !important; text-decoration: underline; }
  .card.contact-card .btn-pro.primary{
    background: linear-gradient(180deg,#4a9fff,#1c62d2) !important;
    color:#fff !important;
  }

  @media (prefers-color-scheme: dark){
    .card.contact-card{
      background: rgba(15,23,42,0.92) !important;
      color:#e5e7eb !important;
      border:1px solid rgba(255,255,255,0.14) !important;
      box-shadow: 0 18px 40px rgba(0,0,0,.42);
    }
    .card.contact-card h1,
    .card.contact-card label{ color:#e5e7eb !important; }
    .card.contact-card .small,
    .card.contact-card .muted{ color:#9aa3b2 !important; }

    .card.contact-card .input,
    .card.contact-card input[type=email],
    .card.contact-card input[type=password]{
      background:#0f172a !important;
      border:1px solid #374151 !important;
      color:#e5e7eb !important;
    }
    .card.contact-card input::placeholder{ color:#9aa3b2 !important; }
    .card.contact-card a{ color:#7db4ff !important; }
  }
</style>
</head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card contact-card">
    <h1>Gratis variant — aanmelden</h1>
    <p class="muted">
      Probeer de service met een gratis account. Geen creditcard, geen automatische verlenging.
      We sturen je een bevestigingsmail; klik op de link en je kunt direct beginnen.
    </p>
    <ul class="muted small" style="margin:12px 0 18px;padding-left:18px;line-height:1.6">
      <li>Tot {{ max_gb }} GB per pakket</li>
      <li>Linkjes maximaal {{ max_days }} dagen geldig</li>
      <li>Maximaal {{ max_active }} actieve pakketten tegelijk</li>
      <li>Wachtwoordbescherming en eigen subdomein zitten in de betaalde variant</li>
    </ul>
    {% if error %}<div class="error" style="background:#fee;border:1px solid #fbb;color:#900;padding:10px 12px;border-radius:8px;margin-bottom:14px">{{ error }}</div>{% endif %}
    {% if info %}<div class="info" style="background:#eff;border:1px solid #bdd;color:#066;padding:10px 12px;border-radius:8px;margin-bottom:14px">{{ info }}</div>{% endif %}
    <form method="post" autocomplete="off" novalidate>
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <label for="email">E-mailadres</label>
      <input id="email" class="input" name="email" type="email" required value="{{ email or '' }}" placeholder="naam@bedrijf.nl">
      <label for="password" style="margin-top:12px">Wachtwoord (min. 10 tekens)</label>
      <input id="password" class="input" name="password" type="password" required minlength="10">
      <p class="muted small" style="margin-top:14px">
        Door op aanmelden te klikken ga je akkoord met onze
        <a href="https://{{ base_host }}/voorwaarden" target="_blank" rel="noopener">voorwaarden</a>.
      </p>
      <button class="btn-pro primary" type="submit" style="margin-top:16px">Aanmelden en bevestigingsmail ontvangen</button>
    </form>
    <p class="muted small" style="margin-top:18px">
      Al een account? <a href="{{ url_for('login') }}">Inloggen</a>.
      Direct upgraden naar de betaalde variant?
      <a href="https://{{ base_host }}/contact?ref=trial_signup_form" target="_blank" rel="noopener">Bekijk de mogelijkheden</a>.
    </p>
  </div>
</div></body></html>
"""

TRIAL_SIGNUP_DONE_HTML = r"""
<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{{ head_icon|safe }}<title>Bevestig je e-mailadres</title>
<style>
  {{ base_css|safe }}
  .card.contact-card{
    background: rgba(255,255,255,0.9) !important;
    color:#0f172a !important;
    border:1px solid rgba(0,0,0,0.08) !important;
    border-radius:20px;
    box-shadow:0 18px 40px rgba(0,0,0,.18);
    backdrop-filter:blur(12px) saturate(1.2);
  }
  .card.contact-card h1{ color:#0f172a !important; }
  .card.contact-card .muted, .card.contact-card .small{ color:#334155 !important; }
  .card.contact-card a{ color:#0f4c98 !important; text-decoration:underline; }
  @media (prefers-color-scheme: dark){
    .card.contact-card{
      background:rgba(15,23,42,0.92) !important;
      color:#e5e7eb !important;
      border:1px solid rgba(255,255,255,0.14) !important;
    }
    .card.contact-card h1{ color:#e5e7eb !important; }
    .card.contact-card .muted, .card.contact-card .small{ color:#9aa3b2 !important; }
    .card.contact-card a{ color:#7db4ff !important; }
  }
</style>
</head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card contact-card">
    <h1>Check je inbox</h1>
    <p>We hebben een bevestigingsmail gestuurd naar <strong>{{ email }}</strong>.</p>
    <p>Klik op de link in de mail om je account te activeren. De link is
       {{ ttl_hours }} uur geldig.</p>
    <p class="muted small">
      Geen mail ontvangen? Check je spam-folder. Of
      <a href="{{ url_for('trial_signup') }}">probeer het opnieuw</a>.
    </p>
  </div>
</div></body></html>
"""

TRIAL_VERIFY_OK_HTML = r"""
<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{{ head_icon|safe }}<title>Account geactiveerd</title>
<style>
  {{ base_css|safe }}
  .card.contact-card{
    background: rgba(255,255,255,0.9) !important;
    color:#0f172a !important;
    border:1px solid rgba(0,0,0,0.08) !important;
    border-radius:20px;
    box-shadow:0 18px 40px rgba(0,0,0,.18);
    backdrop-filter:blur(12px) saturate(1.2);
  }
  .card.contact-card h1{ color:#0f172a !important; }
  .card.contact-card .btn-pro.primary{
    background: linear-gradient(180deg,#4a9fff,#1c62d2) !important;
    color:#fff !important;
  }
  @media (prefers-color-scheme: dark){
    .card.contact-card{
      background:rgba(15,23,42,0.92) !important;
      color:#e5e7eb !important;
      border:1px solid rgba(255,255,255,0.14) !important;
    }
    .card.contact-card h1{ color:#e5e7eb !important; }
  }
</style>
</head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card contact-card">
    <h1>Je account is geactiveerd</h1>
    <p>Je kunt nu inloggen en bestanden delen via de gratis variant.</p>
    <p style="margin-top:18px"><a class="btn-pro primary" href="{{ url_for('login') }}">Inloggen</a></p>
  </div>
</div></body></html>
"""

TRIAL_VERIFY_FAIL_HTML = r"""
<!doctype html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{{ head_icon|safe }}<title>Verificatie mislukt</title>
<style>
  {{ base_css|safe }}
  .card.contact-card{
    background: rgba(255,255,255,0.9) !important;
    color:#0f172a !important;
    border:1px solid rgba(0,0,0,0.08) !important;
    border-radius:20px;
    box-shadow:0 18px 40px rgba(0,0,0,.18);
    backdrop-filter:blur(12px) saturate(1.2);
  }
  .card.contact-card h1{ color:#0f172a !important; }
  .card.contact-card .btn-pro.primary{
    background: linear-gradient(180deg,#4a9fff,#1c62d2) !important;
    color:#fff !important;
  }
  @media (prefers-color-scheme: dark){
    .card.contact-card{
      background:rgba(15,23,42,0.92) !important;
      color:#e5e7eb !important;
      border:1px solid rgba(255,255,255,0.14) !important;
    }
    .card.contact-card h1{ color:#e5e7eb !important; }
  }
</style>
</head><body>
{{ bg|safe }}
<div class="wrap">
  <div class="card contact-card">
    <h1>Deze link werkt niet meer</h1>
    <p>{{ reason }}</p>
    <p style="margin-top:18px"><a class="btn-pro primary" href="{{ url_for('trial_signup') }}">Opnieuw aanmelden</a></p>
  </div>
</div></body></html>
"""

# -------------------- Trial signup route --------------------

@app.route("/trial/signup", methods=["GET", "POST"])
def trial_signup():
    if not TRIAL_ENABLED:
        abort(404)

    base_host = get_base_host()
    ctx = {
        "base_css": BASE_CSS, "bg": BG_DIV, "head_icon": HTML_HEAD_ICON,
        "base_host": base_host,
        "max_gb": int(TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)),
        "max_days": int(TRIAL_MAX_TTL_DAYS),
        "max_active": TRIAL_MAX_ACTIVE_PACKAGES,
    }

    if request.method == "GET":
        return render_template_string(TRIAL_SIGNUP_HTML, error=None, info=None, email="", **ctx)

    ip = _client_ip()
    wait = _rate_is_blocked("trial_signup", ip)
    if wait > 0:
        hrs = max(1, int(wait // 3600))
        return render_template_string(
            TRIAL_SIGNUP_HTML,
            error=f"Te veel aanmeldingen vanaf jouw netwerk. Probeer het over ~{hrs} uur opnieuw.",
            info=None, email="", **ctx
        ), 429

    email = (request.form.get("email") or "").strip().lower()
    pw    = request.form.get("password") or ""

    # Validatie
    if not _EMAIL_RE_TRIAL.match(email) or len(email) > 254:
        return render_template_string(TRIAL_SIGNUP_HTML, error="Vul een geldig e-mailadres in.", info=None, email=email, **ctx), 400
    if _is_disposable_email(email):
        return render_template_string(TRIAL_SIGNUP_HTML, error="Wegwerp-e-mailadressen worden niet geaccepteerd. Gebruik je zakelijke of persoonlijke adres.", info=None, email=email, **ctx), 400
    if len(pw) < 10:
        return render_template_string(TRIAL_SIGNUP_HTML, error="Wachtwoord moet minimaal 10 tekens zijn.", info=None, email=email, **ctx), 400
    if _email_already_in_use(email):
        # Geef een neutrale bevestigingspagina terug om e-mail-enumeratie te
        # voorkomen. Geen feedback "bestaat al"; dat zou attackers helpen.
        return render_template_string(TRIAL_SIGNUP_DONE_HTML, email=email,
                                      ttl_hours=TRIAL_VERIFY_TOKEN_TTL_HRS, **ctx)

    # Genereer verificatie-token (128 bits) en sla pending verificatie op.
    token = secrets.token_hex(16)
    pw_hash = generate_password_hash(pw)
    now_dt = datetime.now(timezone.utc)
    exp_dt = now_dt + timedelta(hours=TRIAL_VERIFY_TOKEN_TTL_HRS)
    conn = db()
    try:
        # Verwijder bestaande niet-geconsumeerde verifs voor dit e-mailadres
        # zodat een tweede signup-poging gewoon werkt en oude tokens dood zijn.
        conn.execute(
            "DELETE FROM email_verifications WHERE email = ? AND consumed_at IS NULL",
            (email,)
        )
        conn.execute(
            """INSERT INTO email_verifications
               (token, email, password_hash, tenant_id, created_at, expires_at, ip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (token, email, pw_hash, TRIAL_TENANT_SLUG,
             now_dt.isoformat(), exp_dt.isoformat(), ip)
        )
        conn.commit()
    finally:
        conn.close()

    _rate_register_failure("trial_signup", ip,
                           TRIAL_SIGNUP_MAX_PER_DAY, TRIAL_SIGNUP_WINDOW, TRIAL_SIGNUP_LOCKOUT)

    # Stuur de verificatiemail. Gebruik TRIAL_HOST als die expliciet gezet is
    # (legacy subdomein-setup), anders het canonieke domein van deze server.
    _verify_host = _trial_host or _tenant_host
    verify_url = f"https://{_verify_host}/trial/verify/{token}"
    _send_verify_email(email, verify_url)

    return render_template_string(TRIAL_SIGNUP_DONE_HTML, email=email,
                                  ttl_hours=TRIAL_VERIFY_TOKEN_TTL_HRS, **ctx)


@app.route("/trial/verify/<token>")
def trial_verify(token):
    if not TRIAL_ENABLED:
        abort(404)

    ctx = {
        "base_css": BASE_CSS, "bg": BG_DIV, "head_icon": HTML_HEAD_ICON,
    }

    # Token-formaat valideren voordat we de DB raken
    if not re.match(r"^[a-f0-9]{32}$", token or ""):
        return render_template_string(TRIAL_VERIFY_FAIL_HTML,
                                      reason="De verificatielink is ongeldig.", **ctx), 400

    conn = db()
    try:
        row = conn.execute(
            "SELECT id, email, password_hash, tenant_id, expires_at, consumed_at "
            "FROM email_verifications WHERE token = ?",
            (token,)
        ).fetchone()
        if row is None:
            return render_template_string(TRIAL_VERIFY_FAIL_HTML,
                                          reason="Deze verificatielink is niet (meer) geldig.", **ctx), 404
        if row["consumed_at"]:
            # Idempotent: al geactiveerd. Stuur door naar success-pagina, niet error.
            return render_template_string(TRIAL_VERIFY_OK_HTML, **ctx)
        exp_dt = parse_dt_utc(row["expires_at"])
        if exp_dt is None or exp_dt <= datetime.now(timezone.utc):
            return render_template_string(TRIAL_VERIFY_FAIL_HTML,
                                          reason="Deze verificatielink is verlopen. Meld je opnieuw aan.", **ctx), 410

        # Race-window: in de tussentijd kan een andere user met hetzelfde adres
        # zijn aangemaakt (bijv. via betaalde flow). Check nogmaals.
        if _email_already_in_use(row["email"]):
            # Markeer als consumed zodat de token dood is.
            conn.execute(
                "UPDATE email_verifications SET consumed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"])
            )
            conn.commit()
            return render_template_string(TRIAL_VERIFY_FAIL_HTML,
                                          reason="Voor dit e-mailadres bestaat al een account. Log in of vraag een wachtwoord-reset aan.", **ctx), 409

        # Maak het user-account aan + markeer token als consumed in één transactie.
        with conn:
            conn.execute(
                """INSERT INTO users (email, password_hash, is_admin, tenant_id,
                                      created_at, disabled, is_trial, email_verified)
                   VALUES (?, ?, 0, ?, ?, 0, 1, 1)""",
                (row["email"], row["password_hash"], row["tenant_id"],
                 datetime.now(timezone.utc).isoformat())
            )
            conn.execute(
                "UPDATE email_verifications SET consumed_at = ? WHERE id = ?",
                (datetime.now(timezone.utc).isoformat(), row["id"])
            )
    finally:
        conn.close()

    return render_template_string(TRIAL_VERIFY_OK_HTML, **ctx)


# -------------- Upload API --------------
@app.route("/package-init", methods=["POST"])
def package_init():
    if not logged_in(): abort(401)
    uid = current_user_id()
    if not uid: abort(401)
    me = current_user()
    is_trial_user = bool(me and me["is_trial"])
    data = request.get_json(force=True, silent=True) or {}
    raw_expiry = data.get("expiry_days")
    is_never = isinstance(raw_expiry, str) and raw_expiry.strip().lower() == "never"

    # Trial-restricties: geen 'never' (onbeperkt), TTL gecapped, geen wachtwoord.
    if is_trial_user:
        if is_never:
            return jsonify(ok=False, error="trial_no_unlimited",
                           message=f"Onbeperkt geldige links zitten in de betaalde variant. Trial-pakketten zijn maximaal {int(TRIAL_MAX_TTL_DAYS)} dagen geldig."), 403
        days = clamp_expiry_days(raw_expiry or TRIAL_MAX_TTL_DAYS)
        days = _enforce_trial_ttl(days)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    elif is_never:
        expires_at = NEVER_EXPIRES_ISO
    else:
        days = clamp_expiry_days(raw_expiry or 24)
        expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    pw   = (data.get("password") or "")[:200]
    if is_trial_user and pw:
        return jsonify(ok=False, error="trial_no_password",
                       message="Wachtwoord-bescherming op pakketten zit in de betaalde variant."), 403

    title_raw = (data.get("title") or "").strip()
    title = title_raw[:MAX_TITLE_LENGTH] if title_raw else None
    token = secrets.token_hex(NEW_TOKEN_BYTES)  # 16 hex = 64 bits entropie
    pw_hash = generate_password_hash(pw) if pw else None
    t = current_tenant()["slug"]
    c = db()
    try:
        # Trial: max aantal actieve pakketten check (hier, in transactie-window).
        if is_trial_user:
            ok, msg = _enforce_trial_active_count(c, uid)
            if not ok:
                return jsonify(ok=False, error="trial_max_active", message=msg), 403
        c.execute("""INSERT INTO packages(token,expires_at,password_hash,created_at,title,tenant_id,owner_user_id)
                     VALUES(?,?,?,?,?,?,?)""",
                  (token, expires_at, pw_hash, datetime.now(timezone.utc).isoformat(), title, t, uid))
        c.commit()
    finally:
        c.close()
    return jsonify(ok=True, token=token)
    
def _user_owns_package(conn, token: str, user_id: int, tenant_slug: str) -> bool:
    """Return True als het pakket van deze user is (admin mag alles binnen tenant)."""
    row = conn.execute(
        "SELECT owner_user_id FROM packages WHERE token = ? AND tenant_id = ?",
        (token, tenant_slug)
    ).fetchone()
    if row is None:
        return False
    if row["owner_user_id"] == user_id:
        return True
    # Admin-fallback: admin mag in eigen tenant alles
    u = current_user()
    if u and u["is_admin"] and u["tenant_id"] == tenant_slug:
        return True
    return False

@app.route("/put-init", methods=["POST"])
def put_init():
    if not logged_in(): abort(401)
    uid = current_user_id()
    if not uid: abort(401)
    me = current_user()
    is_trial_user = bool(me and me["is_trial"])
    d = request.get_json(force=True, silent=True) or {}
    token = (d.get("token") or "").strip(); filename = secure_filename(d.get("filename") or "")
    content_type = (d.get("contentType") or "application/octet-stream").strip() or "application/octet-stream"
    client_size = int(d.get("clientSize") or 0)
    if not is_valid_token(token) or not filename:
        return jsonify(ok=False, error="Onvolledige init (PUT)"), 400
    t = current_tenant()["slug"]
    # Verifieer ownership + trial-grootte-limiet (vooraf weigeren scheelt
    # weggegooid uploadwerk én B2-kosten).
    conn = db()
    try:
        if not _user_owns_package(conn, token, uid, t):
            return jsonify(ok=False, error="forbidden"), 403
        if is_trial_user and client_size > 0:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) AS s FROM items WHERE token = ? AND tenant_id = ?",
                (token, t)
            ).fetchone()
            current_total = int(row["s"] or 0)
            if current_total + client_size > TRIAL_MAX_BYTES_PER_PACKAGE:
                gb = TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)
                return jsonify(ok=False, error="trial_size_limit",
                               message=f"Pakket zou groter worden dan {gb:.0f} GB (trial-limiet). Upgrade voor grotere uploads."), 403
    finally:
        conn.close()
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
    uid = current_user_id()
    if not uid: abort(401)
    d = request.get_json(force=True, silent=True) or {}
    token = (d.get("token") or "").strip(); key = (d.get("key") or "").strip(); name = (d.get("name") or "").strip()
    path  = normalize_rel_path(d.get("path") or name, name)
    if not (is_valid_token(token) and key and name):
        return jsonify(ok=False, error="Onvolledig afronden (PUT)"), 400
    t = current_tenant()["slug"]
    # Verifieer ownership + key-prefix matcht
    if not key.startswith(f"uploads/{t}/{token}/"):
        return jsonify(ok=False, error="invalid_key"), 400
    conn = db()
    try:
        if not _user_owns_package(conn, token, uid, t):
            conn.close()
            return jsonify(ok=False, error="forbidden"), 403
    except Exception:
        conn.close()
        raise
    try:
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
        size = int(head.get("ContentLength", 0))
        # Trial post-check: ondanks pre-check in put-init kan een gemanipuleerde
        # clientSize een te grote upload doorlaten. Hier verifieren we tegen de
        # ECHTE size en ruimen op als de limiet alsnog overschreden wordt.
        me = current_user()
        if me and me["is_trial"]:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) AS s FROM items WHERE token = ? AND tenant_id = ?",
                (token, t)
            ).fetchone()
            current_total = int(row["s"] or 0)
            if current_total + size > TRIAL_MAX_BYTES_PER_PACKAGE:
                gb = TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)
                # Verwijder het zojuist geüploade S3-object — niet in DB inserten.
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=key)
                except Exception:
                    log.exception("trial limit cleanup failed: %s", key)
                conn.close()
                return jsonify(ok=False, error="trial_size_limit",
                               message=f"Pakket overschrijdt {gb:.0f} GB (trial-limiet). Upload geweigerd."), 403
        try:
            conn.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes,tenant_id)
                         VALUES(?,?,?,?,?,?)""",
                      (token, key, name, path, size, t))
            conn.commit()
        except Exception:
            # DB-insert gefaald: ruim S3-object op om wees-object te voorkomen.
            log.exception("put_complete DB insert failed, deleting orphan S3 object: %s", key)
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                log.exception("orphan delete failed: %s", key)
            raise
        conn.close()
        return jsonify(ok=True)
    except (ClientError, BotoCoreError):
        conn.close()
        log.exception("put_complete failed")
        return jsonify(ok=False, error="server_error"), 500

@app.route("/mpu-init", methods=["POST"])
def mpu_init():
    if not logged_in(): abort(401)
    uid = current_user_id()
    if not uid: abort(401)
    me = current_user()
    is_trial_user = bool(me and me["is_trial"])
    data = request.get_json(force=True, silent=True) or {}
    token = (data.get("token") or "").strip()
    filename = secure_filename(data.get("filename") or "")
    content_type = (data.get("contentType") or "application/octet-stream").strip() or "application/octet-stream"
    client_size = int(data.get("clientSize") or 0)
    if not is_valid_token(token) or not filename:
        return jsonify(ok=False, error="Onvolledige init (MPU)"), 400
    t = current_tenant()["slug"]
    conn = db()
    try:
        if not _user_owns_package(conn, token, uid, t):
            return jsonify(ok=False, error="forbidden"), 403
        if is_trial_user and client_size > 0:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) AS s FROM items WHERE token = ? AND tenant_id = ?",
                (token, t)
            ).fetchone()
            current_total = int(row["s"] or 0)
            if current_total + client_size > TRIAL_MAX_BYTES_PER_PACKAGE:
                gb = TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)
                return jsonify(ok=False, error="trial_size_limit",
                               message=f"Pakket zou groter worden dan {gb:.0f} GB (trial-limiet). Upgrade voor grotere uploads."), 403
    finally:
        conn.close()
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
    uid = current_user_id()
    if not uid: abort(401)
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key"); upload_id = data.get("uploadId")
    part_no = int(data.get("partNumber") or 0)
    if not key or not upload_id or part_no<=0:
        return jsonify(ok=False, error="Onvolledig sign"), 400
    # Key moet binnen tenant zijn en token eruit halen om ownership te checken
    t = current_tenant()["slug"]
    prefix = f"uploads/{t}/"
    if not key.startswith(prefix):
        return jsonify(ok=False, error="invalid_key"), 400
    rest = key[len(prefix):]
    token_from_key = rest.split("/", 1)[0] if "/" in rest else ""
    if not is_valid_token(token_from_key):
        return jsonify(ok=False, error="invalid_key"), 400
    conn = db()
    try:
        if not _user_owns_package(conn, token_from_key, uid, t):
            return jsonify(ok=False, error="forbidden"), 403
    finally:
        conn.close()
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
    uid = current_user_id()
    if not uid: abort(401)
    data      = request.get_json(force=True, silent=True) or {}
    token     = (data.get("token") or "").strip(); key = (data.get("key") or "").strip()
    name      = (data.get("name") or "").strip();  path = normalize_rel_path(data.get("path") or name, name)
    parts_in  = data.get("parts") or []; upload_id = data.get("uploadId")
    client_size = int(data.get("clientSize") or 0)
    if not (is_valid_token(token) and key and name and parts_in and upload_id):
        return jsonify(ok=False, error="Onvolledig afronden (ontbrekende velden)"), 400
    t = current_tenant()["slug"]
    if not key.startswith(f"uploads/{t}/{token}/"):
        return jsonify(ok=False, error="invalid_key"), 400
    conn = db()
    try:
        if not _user_owns_package(conn, token, uid, t):
            conn.close()
            return jsonify(ok=False, error="forbidden"), 403
    except Exception:
        conn.close()
        raise
    # Stap 1: MPU afronden. Als dit faalt, abort de MPU zodat er geen weeszones overblijven.
    try:
        s3.complete_multipart_upload(
            Bucket=S3_BUCKET, Key=key,
            MultipartUpload={"Parts": sorted(parts_in, key=lambda p: p["PartNumber"])},
            UploadId=upload_id
        )
    except (ClientError, BotoCoreError):
        log.exception("mpu_complete failed")
        try:
            s3.abort_multipart_upload(Bucket=S3_BUCKET, Key=key, UploadId=upload_id)
        except Exception:
            log.exception("mpu abort after complete-fail failed: %s", key)
        conn.close()
        return jsonify(ok=False, error="mpu_complete_failed"), 500
    # Stap 2: head_object voor groottebepaling.
    try:
        size = 0
        try:
            head = s3.head_object(Bucket=S3_BUCKET, Key=key)
            size = int(head.get("ContentLength", 0))
        except Exception:
            if client_size>0: size = client_size
            else: raise
        # Trial post-check: zie put_complete. Bij overschrijding ruim de
        # zojuist gemerge-de MPU op via delete_object.
        me = current_user()
        if me and me["is_trial"]:
            row = conn.execute(
                "SELECT COALESCE(SUM(size_bytes),0) AS s FROM items WHERE token = ? AND tenant_id = ?",
                (token, t)
            ).fetchone()
            current_total = int(row["s"] or 0)
            if current_total + size > TRIAL_MAX_BYTES_PER_PACKAGE:
                gb = TRIAL_MAX_BYTES_PER_PACKAGE / (1024**3)
                try:
                    s3.delete_object(Bucket=S3_BUCKET, Key=key)
                except Exception:
                    log.exception("trial limit cleanup failed: %s", key)
                conn.close()
                return jsonify(ok=False, error="trial_size_limit",
                               message=f"Pakket overschrijdt {gb:.0f} GB (trial-limiet). Upload geweigerd."), 403
        # Stap 3: DB insert. Als deze faalt, ruim het S3-object op anders wees-object in B2.
        try:
            conn.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes,tenant_id)
                         VALUES(?,?,?,?,?,?)""",
                      (token, key, name, path, size, t))
            conn.commit()
        except Exception:
            log.exception("mpu_complete DB insert failed, deleting orphan S3 object: %s", key)
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=key)
            except Exception:
                log.exception("orphan delete failed: %s", key)
            raise
        conn.close()
        return jsonify(ok=True)
    except Exception:
        conn.close()
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
    supplied = request.headers.get("X-Task-Token", "")
    if not task_token or not hmac.compare_digest(supplied, task_token):
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


@app.get("/internal/test-mail")
def internal_test_mail():
    """
    Tijdelijke diagnostische route om SMTP-config te verifiëren.

    Stuurt synchroon één test-mail; eventuele fouten komen direct terug in de
    response in plaats van te verdwijnen in een achtergrond-thread-log.

    Auth via query: ?token=<TASK_TOKEN>
    Bestemming via query: ?to=<emailadres>  (default: MAIL_TO)

    Voorbeeld:
      curl -s "https://downloadlink.nl/internal/test-mail?token=$TASK_TOKEN&to=jij@example.com"

    Response (succes):
      {"ok": true, "to": "jij@example.com", "from": "noreply@...",
       "smtp_host": "...", "smtp_port": 587}

    Response (fout):
      {"ok": false, "error": "<smtplib uitleg>"}
    """
    task_token = os.environ.get("TASK_TOKEN")
    supplied = request.args.get("token", "")
    if not task_token or not hmac.compare_digest(supplied, task_token):
        return ("Forbidden", 403)

    to_addr = (request.args.get("to") or MAIL_TO or "").strip()
    if not to_addr:
        return jsonify(
            ok=False,
            error="Geen ontvanger. Gebruik ?to=jij@example.com of zet MAIL_TO."
        ), 400

    # Config-zichtbaarheid (zonder secrets te lekken)
    cfg = {
        "smtp_host": SMTP_HOST or "(leeg)",
        "smtp_port": SMTP_PORT,
        "smtp_user_set": bool(SMTP_USER),
        "smtp_pass_set": bool(SMTP_PASS),
        "smtp_from": SMTP_FROM or "(leeg)",
        "to": to_addr,
    }

    if not (SMTP_HOST and SMTP_USER and SMTP_PASS):
        return jsonify(
            ok=False,
            error="SMTP niet (volledig) geconfigureerd. Check SMTP_HOST, SMTP_USER, SMTP_PASS.",
            **cfg
        ), 500

    subject = "Test-mail vanuit downloadlink.nl"
    body = (
        "Dit is een diagnostische test-mail.\n\n"
        f"Verzonden vanaf host: {request.host}\n"
        f"Tijdstip (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
        "Als je deze mail ontvangt, werkt de SMTP-configuratie correct.\n"
    )

    try:
        # Synchroon aanroepen — geen send_email_async — zodat we de fout
        # direct in de response kunnen tonen.
        send_email(to_addr, subject, body)
    except Exception as e:
        logging.exception("internal_test_mail send failed")
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}", **cfg), 500

    return jsonify(ok=True, **cfg)


# -------------- Download Pages --------------
# Background executor voor niet-blokkerende S3 cleanup bij expired packages
_bg_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="bg")

# TTL voor pakket-wachtwoord sessies (seconden).
PKG_ALLOW_TTL = int(os.environ.get("PKG_ALLOW_TTL", "3600"))  # 1 uur

def _pkg_allow_is_valid(token: str) -> bool:
    rec = session.get(f"allow_{token}")
    if not rec:
        return False
    # Backwards-compat: oude sessies hadden gewoon True
    if rec is True:
        session.pop(f"allow_{token}", None)
        return False
    if not isinstance(rec, dict):
        return False
    return rec.get("ok") is True and float(rec.get("exp") or 0) > time.time()

# Maximaal aantal pakket-toegang sleutels in de sessie. Voorkomt dat een
# Flask-sessie cookie (max ~4KB) volloopt bij gebruikers die veel
# password-protected pakketten openen.
_PKG_ALLOW_MAX_KEYS = 25

def _pkg_allow_set(token: str) -> None:
    # Verwijder eerst verlopen entries; cap daarna op de max.
    now_ts = time.time()
    allow_keys = [k for k in list(session.keys()) if k.startswith("allow_")]
    for k in allow_keys:
        rec = session.get(k)
        if not isinstance(rec, dict) or float(rec.get("exp") or 0) <= now_ts:
            session.pop(k, None)
    # Hertel na opschoning
    allow_keys = [k for k in list(session.keys()) if k.startswith("allow_")]
    # Als we nog over de limiet zitten, gooi de oudste (laagste exp) eruit
    if len(allow_keys) >= _PKG_ALLOW_MAX_KEYS:
        sortable = []
        for k in allow_keys:
            rec = session.get(k) or {}
            sortable.append((float(rec.get("exp") or 0), k))
        sortable.sort()
        for _, k in sortable[:len(allow_keys) - _PKG_ALLOW_MAX_KEYS + 1]:
            session.pop(k, None)
    session[f"allow_{token}"] = {"ok": True, "exp": now_ts + PKG_ALLOW_TTL}

def _async_delete_s3_keys(keys: list) -> None:
    """Delete objecten op achtergrond, gebatched.

    `delete_objects` neemt tot 1000 keys per call. Voor grote pakketten
    (1000+ items) is dat 100x sneller dan losse `delete_object` calls,
    wat anders de bg-executor minutenlang kon bezighouden.
    """
    if not keys:
        return
    # Filter en de-dup keys defensief
    keys = [k for k in dict.fromkeys(keys) if k]
    if not keys:
        return

    def _do():
        BATCH = 1000
        for i in range(0, len(keys), BATCH):
            chunk = keys[i:i + BATCH]
            try:
                resp = s3.delete_objects(
                    Bucket=S3_BUCKET,
                    Delete={
                        "Objects": [{"Key": k} for k in chunk],
                        "Quiet": True,
                    },
                )
                # B2/S3 kan per-key errors teruggeven zonder de hele call te falen.
                errors = resp.get("Errors") or []
                if errors:
                    log.warning("bg delete: %d/%d keys faalden in batch", len(errors), len(chunk))
                    # Probeer de gefaalde keys nog één keer individueel — soms is
                    # het een transient fout (rate limit op een specifieke key).
                    for e in errors:
                        k = e.get("Key")
                        if not k:
                            continue
                        try:
                            s3.delete_object(Bucket=S3_BUCKET, Key=k)
                        except Exception:
                            log.exception("bg delete retry failed: %s", k)
            except (ClientError, BotoCoreError):
                # Provider ondersteunt delete_objects mogelijk niet of weigert de
                # batch — fallback naar één-voor-één voor deze chunk.
                log.exception("bg delete_objects batch failed, falling back to single deletes")
                for k in chunk:
                    try:
                        s3.delete_object(Bucket=S3_BUCKET, Key=k)
                    except Exception:
                        log.exception("bg delete failed: %s", k)
    try:
        _bg_executor.submit(_do)
    except Exception:
        log.exception("bg submit failed")

@app.route("/p/arcade")
def arcade_redirect():
    return redirect("/arcade", code=302)

@app.route("/p/<token>", methods=["GET","POST"])
def package_page(token):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)

    # Rate limit op pakket-views per IP (voorkomt brute-force op tokens)
    ip = _client_ip()
    wait = _rate_is_blocked("pkgview", ip)
    if wait > 0:
        abort(429)

    c = db()
    try:
        t = current_tenant()["slug"]
        pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
        if not pkg:
            _rate_register_failure("pkgview", ip, PKGVIEW_MAX_ATTEMPTS, PKGVIEW_WINDOW_SECONDS, PKGVIEW_LOCKOUT_SECONDS)
            return render_template_string(
                EXPIRED_HTML,
                base_css=BASE_CSS,
                bg=BG_DIV,
                head_icon=HTML_HEAD_ICON
            ), 404

        # Robuuste datetime-vergelijking: bij parse-fout behandelen we het pakket
        # als verlopen (veiliger dan een 500). Onbeperkte pakketten (jaar 9999)
        # blijven geldig omdat die ver in de toekomst staan.
        now_utc = datetime.now(timezone.utc)
        exp_dt = parse_dt_utc(pkg["expires_at"]) or now_utc
        if exp_dt <= now_utc:
            # Atomair: items + package in één transactie verwijderen, dan async
            # de S3-keys opruimen. Voorkomt half-opgeruimde state bij crash.
            try:
                rows = c.execute("SELECT s3_key FROM items WHERE token=? AND tenant_id=?", (token, t)).fetchall()
                s3_keys = [r["s3_key"] for r in rows]
                with c:  # commit/rollback transactie
                    c.execute("DELETE FROM items WHERE token=? AND tenant_id=?", (token, t))
                    c.execute("DELETE FROM packages WHERE token=? AND tenant_id=?", (token, t))
            except sqlite3.Error:
                log.exception("expired package cleanup DB failed")
                s3_keys = []
            if s3_keys:
                _async_delete_s3_keys(s3_keys)
            abort(410)

        # Pakket-bescherming voor ingelogde gebruikers: per pakket éénmalig
        # wachtwoord vragen, zodat collega's niet ongezien rondkijken.
        if _needs_cross_user_unlock(pkg):
            return redirect(url_for("cross_user_unlock", token=token, next=request.path))

        if pkg["password_hash"]:
            if request.method == "GET" and not _pkg_allow_is_valid(token):
                return render_template_string(PASS_PROMPT_HTML, base_css=BASE_CSS, bg=BG_DIV, error=None, head_icon=HTML_HEAD_ICON)
            if request.method == "POST":
                if not check_password_hash(pkg["password_hash"], request.form.get("password","")):
                    _rate_register_failure("pkgview", ip, PKGVIEW_MAX_ATTEMPTS, PKGVIEW_WINDOW_SECONDS, PKGVIEW_LOCKOUT_SECONDS)
                    return render_template_string(PASS_PROMPT_HTML, base_css=BASE_CSS, bg=BG_DIV, error="Onjuist wachtwoord. Probeer opnieuw.", head_icon=HTML_HEAD_ICON)
                _pkg_allow_set(token)

        items = c.execute("""SELECT id,name,path,size_bytes FROM items
                             WHERE token=? AND tenant_id=?
                             ORDER BY path""", (token, t)).fetchall()
    finally:
        c.close()

    total_bytes = sum(int(r["size_bytes"]) for r in items)
    total_h = human(total_bytes)
    dt = (parse_dt_utc(pkg["expires_at"]) or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    if dt.year >= 9000:
        expires_h = "Onbeperkt geldig"
    else:
        expires_h = dt.strftime("%d-%m-%Y %H:%M")

    its = [{"id":r["id"], "name":r["name"], "path":r["path"], "size_h":human(int(r["size_bytes"]))} for r in items]

    return render_template_string(
        PACKAGE_HTML,
        token=token, title=pkg["title"],
        items=its, total_human=total_h,
        expires_human=expires_h, base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON,
        base_host=get_base_host()
    )

# ----------------------------------------------------------------------
# Pakket-bescherming voor ingelogde gebruikers (gedeeld account)
# ----------------------------------------------------------------------
# Eén login wordt door meerdere collega's gedeeld. Om te voorkomen dat
# een collega 'even rondkijkt' in de uploads van een ander, moet hij —
# als hij vanuit 'Mijn uploads' een pakket opent — éérst het globale
# wachtwoord invoeren. Per pakket apart (geen globale unlock), zodat
# één keer invoeren niet meteen het hele archief opent.
#
# Externe ontvangers (niet ingelogd, alleen de share-link in handen)
# merken hier niets van — die volgen het normale pad.
#
# De check geldt alleen als:
#   1. MY_UPLOADS_PASSWORD is geconfigureerd, EN
#   2. de bezoeker ingelogd is, EN
#   3. dit specifieke pakket nog niet ontgrendeld is in deze sessie.

CROSS_USER_UNLOCK_KEY = "pkg_unlocked_tokens"  # dict: {token: exp_timestamp}

def _get_unlocked_tokens() -> dict:
    """Haal de dict met ontgrendelde tokens uit de sessie. Maakt schoon
    onderweg (verwijdert verlopen entries)."""
    raw = session.get(CROSS_USER_UNLOCK_KEY) or {}
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    cleaned = {tok: exp for tok, exp in raw.items()
               if isinstance(exp, (int, float)) and exp > now}
    # Schrijf alleen terug als er daadwerkelijk iets veranderd is,
    # om onnodige session-cookie-updates te voorkomen.
    if len(cleaned) != len(raw):
        session[CROSS_USER_UNLOCK_KEY] = cleaned
    return cleaned

def _is_token_unlocked(token: str) -> bool:
    """True als dit specifieke pakket in de huidige sessie al is ontgrendeld."""
    return token in _get_unlocked_tokens()

def _set_token_unlocked(token: str) -> None:
    """Markeer een pakket als ontgrendeld in deze sessie."""
    tokens = _get_unlocked_tokens()
    tokens[token] = time.time() + MY_UPLOADS_UNLOCK_TTL
    session[CROSS_USER_UNLOCK_KEY] = tokens

def _needs_cross_user_unlock(pkg) -> bool:
    """True als deze request een ingelogde-gebruiker is die dit specifieke
    pakket nog niet heeft ontgrendeld in zijn sessie. False voor anonieme
    bezoekers (externe ontvangers) en als de feature uit staat."""
    if not MY_UPLOADS_PASSWORD:
        return False
    if not logged_in():
        return False  # externe bezoeker met share-link: niet blokkeren
    try:
        token = pkg["token"]
    except (KeyError, IndexError, TypeError):
        return False  # zonder token kunnen we niet matchen; veiliger om door te laten
    return not _is_token_unlocked(token)

CROSS_USER_UNLOCK_HTML = """<!doctype html>
<html lang="nl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  {{ head_icon|safe }}
  <meta name="csrf-token" content="{{ csrf_token() }}"/>
  <title>Toegang vereist</title>
  <style>{{ base_css|safe }}</style>
  <style>
    .unlock-card { max-width: 440px; margin: 6vh auto; padding: 28px; }
    .unlock-card h1 { margin: 0 0 8px; font-size: 22px; }
    .unlock-card p.lead { color: var(--muted, #6b7280); margin: 0 0 18px; font-size: 14px; }
    .unlock-card label { display:block; font-size: 13px; margin: 12px 0 6px; font-weight: 600; }
    .unlock-card input[type="password"] {
      width: 100%; padding: 10px 12px; border-radius: 8px;
      border: 1px solid rgba(0,0,0,.15); font-size: 15px; box-sizing: border-box;
    }
    .unlock-card .actions { margin-top: 18px; display: flex; gap: 8px; justify-content: flex-end; }
    .unlock-card .err {
      background:#fee2e2; color:#991b1b; padding:8px 12px;
      border-radius:8px; font-size:13px; margin-bottom: 12px;
    }
  </style>
</head>
<body>
  {{ bg|safe }}
  <div class="shell">
    <div class="card unlock-card">
      <h1>🔒 Toegang vereist</h1>
      <p class="lead">Voer het wachtwoord in om dit pakket te bekijken.</p>
      {% if error %}<div class="err">{{ error }}</div>{% endif %}
      <form method="post" action="{{ url_for('cross_user_unlock') }}" autocomplete="off">
        <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
        <input type="hidden" name="token" value="{{ token }}">
        <input type="hidden" name="next" value="{{ next_url }}">
        <label for="pw">Wachtwoord</label>
        <input type="password" id="pw" name="password" autofocus required>
        <div class="actions">
          <a class="btn-pro" href="{{ url_for('my_uploads') }}">Annuleren</a>
          <button class="btn-pro primary" type="submit">Ontgrendelen</button>
        </div>
      </form>
    </div>
  </div>
</body>
</html>"""

def _render_cross_user_unlock(token="", error=None, next_url=None):
    safe_next = next_url or url_for("my_uploads")
    return render_template_string(
        CROSS_USER_UNLOCK_HTML,
        token=token or "",
        error=error,
        next_url=safe_next,
        base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON
    )

def _safe_next_url(raw: str, fallback: str) -> str:
    """Alleen relatieve next-URL's accepteren (geen open redirect)."""
    raw = (raw or "").strip()
    if raw.startswith("/") and not raw.startswith("//"):
        return raw
    return fallback

@app.route("/p/unlock", methods=["GET", "POST"])
def cross_user_unlock():
    # Niet-ingelogde bezoekers horen hier helemaal niet te zijn —
    # voor hen geldt deze bescherming niet.
    if not logged_in():
        return redirect(url_for("login"))
    if not MY_UPLOADS_PASSWORD:
        return redirect(url_for("my_uploads"))

    token_raw = (request.values.get("token") or "").strip().lower()
    # Defensief: alleen ontgrendelen als token-vorm valide is.
    if not is_valid_token(token_raw):
        # Geen geldige token meegegeven: stuur door zonder iets te ontgrendelen.
        return redirect(url_for("my_uploads"))

    next_url = _safe_next_url(
        request.values.get("next"),
        url_for("package_page", token=token_raw)
    )

    if request.method == "POST":
        submitted = request.form.get("password") or ""
        if hmac.compare_digest(submitted, MY_UPLOADS_PASSWORD):
            _set_token_unlocked(token_raw)
            return redirect(next_url)
        return _render_cross_user_unlock(
            token=token_raw,
            error="Onjuist wachtwoord. Probeer het opnieuw.",
            next_url=next_url
        )

    return _render_cross_user_unlock(token=token_raw, next_url=next_url)


def _is_pkg_expired(pkg) -> bool:
    """Robuust check: True als pakket verlopen is. Behandelt parse-fouten als verlopen."""
    now_utc = datetime.now(timezone.utc)
    exp_dt = parse_dt_utc(pkg["expires_at"])
    if exp_dt is None:
        return True  # niet-parseable: veiliger om als verlopen te zien
    return exp_dt <= now_utc

def _safe_content_disposition(filename: str) -> str:
    """Bouw een RFC 6266-conforme Content-Disposition waarde.

    - Strip CR/LF (defense-in-depth tegen response splitting)
    - Strip quote-characters
    - Geeft naast `filename=` ook `filename*=UTF-8''...` zodat browsers
      non-ASCII titels (bijv. 'België-rapport.zip') correct tonen.
    - ASCII-fallback met onleesbare bytes vervangen door '_'.
    """
    from urllib.parse import quote as _quote
    raw = (filename or "download").replace("\r", "").replace("\n", "").replace('"', "").strip()
    if not raw:
        raw = "download"
    # ASCII-fallback voor oude clients
    ascii_name = raw.encode("ascii", "replace").decode("ascii").replace("?", "_")
    # RFC 5987 percent-encoding voor de UTF-8-variant
    utf8_name = _quote(raw, safe="")
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{utf8_name}"

@app.route("/file/<token>/<int:item_id>")
def stream_file(token, item_id):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)
    c = db()
    try:
        t = current_tenant()["slug"]
        pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
        if not pkg: abort(404)
        if _is_pkg_expired(pkg): abort(410)
        # Pakket-bescherming: collega kan zonder unlock niet rechtstreeks downloaden.
        if _needs_cross_user_unlock(pkg):
            return redirect(url_for("cross_user_unlock", token=token, next=url_for("package_page", token=token)))
        if pkg["password_hash"] and not _pkg_allow_is_valid(token): abort(403)
        it = c.execute("SELECT * FROM items WHERE id=? AND token=? AND tenant_id=?", (item_id, token, t)).fetchone()
    finally:
        c.close()
    if not it: abort(404)

    log_download_event(
        token=token,
        tenant_id=t,
        download_type="file",
        item_id=it["id"]
    )

    # Presigned GET: browser praat direct met B2/S3. Scheelt bandbreedte en voorkomt
    # dat request-tijd de Render-timeout raakt bij grote bestanden.
    try:
        # RFC 6266-conforme Content-Disposition met UTF-8-fallback voor
        # non-ASCII filenames (bijv. accenten, Arabisch, Chinees).
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": S3_BUCKET,
                "Key": it["s3_key"],
                "ResponseContentDisposition": _safe_content_disposition(it["name"] or "download"),
            },
            ExpiresIn=3600, HttpMethod="GET",
        )
        return redirect(url, code=302)
    except Exception:
        log.exception("stream_file presign failed")
        abort(500)

@app.route("/zip/<token>")
def stream_zip(token):
    token = (token or "").strip()
    if not is_valid_token(token): abort(404)
    c = db()
    try:
        t = current_tenant()["slug"]
        pkg = c.execute("SELECT * FROM packages WHERE token=? AND tenant_id=?", (token, t)).fetchone()
        if not pkg: abort(404)
        if _is_pkg_expired(pkg): abort(410)
        # Pakket-bescherming: collega kan zonder unlock niet rechtstreeks downloaden.
        if _needs_cross_user_unlock(pkg):
            return redirect(url_for("cross_user_unlock", token=token, next=url_for("package_page", token=token)))
        if pkg["password_hash"] and not _pkg_allow_is_valid(token): abort(403)
        rows = c.execute("""SELECT name,path,s3_key FROM items
                            WHERE token=? AND tenant_id=?
                            ORDER BY path""", (token, t)).fetchall()
    finally:
        c.close()
    if not rows: abort(404)

    log_download_event(
        token=token,
        tenant_id=t,
        download_type="zip",
        item_id=None
    )

    # Geen aparte precheck meer: dat kostte bij grote pakketten 1-3s extra
    # vóór de eerste byte. Eventuele NoSuchKey-fouten komen tijdens streaming
    # naar boven en worden door de exception-handler hieronder gevangen.
    try:
        from queue import Queue
        from threading import Thread

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

        # Detecteer de werkende zipstream-ng add()-signatuur ÉÉN keer met een
        # lege probe-entry, niet bij elk bestand. Bij 1000+ bestanden scheelt
        # dit substantieel CPU (geen exception-traceback bouwen × N).
        # We schrijven de probe niet daadwerkelijk naar de zip — we gebruiken
        # een wegwerp-ZipStream alleen voor signature-detection.
        def _detect_add_strategy():
            probe = ZipStream()
            def empty_iter():
                if False: yield b""
            strategies = []
            if hasattr(probe, "add_iter"):
                strategies.append(("add_iter", lambda zz, name, gf: zz.add_iter(name, gf())))
            strategies.extend([
                ("add_iterable", lambda zz, name, gf: zz.add(arcname=name, iterable=gf())),
                ("add_stream",   lambda zz, name, gf: zz.add(arcname=name, stream=gf())),
                ("add_fileobj",  lambda zz, name, gf: zz.add(arcname=name, fileobj=_GenReader(gf()))),
                ("add_pos",      lambda zz, name, gf: zz.add(name, gf())),
                ("add_pos_rev",  lambda zz, name, gf: zz.add(gf(), name)),
            ])
            for name, fn in strategies:
                try:
                    fn(probe, "__probe__", empty_iter)
                    return fn
                except Exception:
                    continue
            raise RuntimeError("Geen compatibele zipstream-ng add() signatuur gevonden")

        _add_iter_strategy = _detect_add_strategy()

        def add_compat(arcname, gen_factory):
            # Hot path: directe call naar de gedetecteerde strategie.
            _add_iter_strategy(z, arcname, gen_factory)

        # --- Prefetch pipeline ---
        # Bij veel kleine bestanden zit de tijd in S3-latency per object, niet in
        # bandbreedte. We halen daarom meerdere objecten parallel op terwijl de
        # zipstream sequentieel blijft schrijven (zip-formaat eist seriele output).
        PREFETCH_WORKERS = int(os.environ.get("ZIP_PREFETCH_WORKERS", "8"))
        PREFETCH_QUEUE   = int(os.environ.get("ZIP_PREFETCH_QUEUE", "16"))
        # Voor kleine objecten loont het om de bytes volledig in geheugen te
        # materialiseren (1 round-trip, geen chunk-overhead). Boven deze drempel
        # streamen we alsnog in chunks om RAM te sparen.
        SMALL_FILE_BYTES = int(os.environ.get("ZIP_SMALL_FILE_BYTES", str(8 * 1024 * 1024)))

        # Bounded queue: blokkeert producers als de zip-consumer achterloopt,
        # zodat we nooit meer dan ~PREFETCH_QUEUE bestanden tegelijk in RAM hebben.
        q = Queue(maxsize=PREFETCH_QUEUE)
        SENTINEL = object()
        producer_error = {}

        def _fetch_one(key):
            obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
            length = obj.get("ContentLength")
            body = obj["Body"]
            if length is not None and length <= SMALL_FILE_BYTES:
                # Klein bestand: 1 keer lezen en doorgeven als bytes.
                return ("bytes", body.read())
            # Groot bestand: stream via een interne chunk-queue zodat de download
            # alvast doorloopt terwijl de zip nog vorige bestanden verwerkt.
            chunk_q = Queue(maxsize=8)
            def _stream():
                try:
                    for chunk in body.iter_chunks(1024 * 1024):
                        if chunk: chunk_q.put(chunk)
                except Exception as ex:
                    chunk_q.put(("__ERR__", ex))
                finally:
                    chunk_q.put(None)
            Thread(target=_stream, daemon=True).start()

            def _gen():
                while True:
                    item = chunk_q.get()
                    if item is None: return
                    if isinstance(item, tuple) and item and item[0] == "__ERR__":
                        raise item[1]
                    yield item
            return ("stream", _gen)

        def _producer():
            try:
                with ThreadPoolExecutor(max_workers=PREFETCH_WORKERS) as pool:
                    # Submit alle fetches; result() opvragen we sequentieel zodat
                    # de zip de bestanden in dezelfde volgorde toegevoegd krijgt.
                    futures = [(r, pool.submit(_fetch_one, r["s3_key"])) for r in rows]
                    for r, fut in futures:
                        arcname = r["path"] or r["name"]
                        try:
                            kind, payload = fut.result()
                        except Exception as ex:
                            producer_error["err"] = ex
                            return
                        q.put((arcname, kind, payload))
            except Exception as ex:
                producer_error["err"] = ex
            finally:
                q.put(SENTINEL)

        Thread(target=_producer, daemon=True).start()

        # Detecteer 1 keer of de bibliotheek een directe bytes-API heeft.
        # Sommige zipstream-ng versies accepteren `data=` als kwarg en kunnen
        # dan zonder generator-overhead bytes inserten.
        _bytes_api_works = False
        try:
            _probe_z = ZipStream()
            _probe_z.add(arcname="__probe_bytes__", data=b"")
            _bytes_api_works = True
        except Exception:
            _bytes_api_works = False

        # Consumer: voeg in volgorde toe aan de zipstream.
        while True:
            item = q.get()
            if item is SENTINEL:
                if "err" in producer_error:
                    raise producer_error["err"]
                break
            arcname, kind, payload = item
            if kind == "bytes":
                if _bytes_api_works:
                    z.add(arcname=arcname, data=payload)
                else:
                    add_compat(arcname, lambda data=payload: iter([data]))
            else:
                add_compat(arcname, payload)

        def generate():
            for chunk in z: yield chunk

        filename = (pkg["title"] or f"onderwerp-{token}").strip()
        if not filename.lower().endswith(".zip"): filename += ".zip"
        # Strip CR/LF expliciet als extra safety net voor X-Filename header.
        x_filename = filename.replace("\r", "").replace("\n", "").replace('"', "")

        resp = Response(stream_with_context(generate()), mimetype="application/zip")
        resp.headers["Content-Disposition"] = _safe_content_disposition(filename)
        resp.headers["X-Filename"] = x_filename
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
# Iets strenger dan 'ooit een @ en een .' — valideert lengtes en voorkomt
# meerdere @ of punt aan begin/eind van domein. Voor echte RFC-validatie zou
# email-validator (pip) robuuster zijn, maar dit dekt 99% van de gevallen.
EMAIL_RE = re.compile(
    r"^(?=.{3,254}$)"
    r"[A-Za-z0-9._%+\-]{1,64}"
    r"@"
    r"[A-Za-z0-9]([A-Za-z0-9\-]{0,62}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,62}[A-Za-z0-9])?)+$"
)
PHONE_RE  = re.compile(r"^[0-9+()\\s-]{8,20}$")
ALLOWED_TB = {0.5, 1.0, 2.0, 5.0}
PRICE_LABEL = {0.5:"€12/maand", 1.0:"€15/maand", 2.0:"€20/maand", 5.0:"€30/maand"}

# Mapping van interne ref-codes naar leesbare bron-labels in de mailmelding.
# Onbekende waarden worden doorgegeven zoals ze zijn (al gefilterd op alphanumeric).
_REF_LABELS = {
    "direct":    "rechtstreeks via /contact (geen ref)",
    "p_postdl":  "downloadpagina – kaart na succesvolle download",
}

def _describe_ref(ref: str) -> str:
    return _REF_LABELS.get(ref, f"onbekende bron: {ref}")

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

    # Wachtwoord wordt server-side gehasht (pbkdf2) en er wordt een pending_account
    # opgeslagen. Als de klant via PayPal betaalt, wordt het account automatisch
    # aangemaakt bij de BILLING.SUBSCRIPTION.ACTIVATED webhook. De hash wordt ook
    # in deze mail meegestuurd zodat je handmatig kunt aanmaken als de klant níet
    # via PayPal betaalt (bijv. factuur-per-e-mail).
    desired_pw = form.get("desired_password") or ""
    if desired_pw:
        pw_hash = generate_password_hash(desired_pw)
        pw_block = (
            "- Wachtwoord: ingesteld door klant (zie hash hieronder)\n"
            f"- Wachtwoord-hash: {pw_hash}\n"
        )
        instructions = (
            "\n"
            "==== Activatie ====\n"
            "Als de klant via PayPal betaalt: account wordt AUTOMATISCH aangemaakt\n"
            "zodra de ACTIVATED-webhook binnenkomt. Je hoeft niets te doen.\n"
            "\n"
            "Als je het account handmatig wilt aanmaken (bijv. voor testing of\n"
            "factuur-per-e-mail):\n"
            "1. Log in op het admin-paneel (/admin/users)\n"
            "2. Klap open: \"Aanmaken via wachtwoord-hash (uit contactmail)\"\n"
            f"3. Vul in: e-mail = {form['login_email']}\n"
            "4. Plak de bovenstaande Wachtwoord-hash in het hash-veld\n"
            "5. Klik \"Aanmaken met hash\"\n"
            "\n"
            "De klant kan in beide gevallen direct inloggen met het wachtwoord\n"
            "dat hij zelf heeft ingevuld op het contactformulier.\n"
        )
    else:
        pw_block = "- Wachtwoord: (niet ingevuld)\n"
        instructions = ""

    body = (
        "Er is een nieuwe aanvraag binnengekomen:\n\n"
        f"- Gewenste inlog-e-mail: {form['login_email']}\n"
        f"{storage_line}"
        f"- Bedrijfsnaam: {form['company']}\n"
        f"- Telefoonnummer: {form['phone']}\n"
        f"{pw_block}"
        f"- Subdomein voorbeeld: {example_link}\n"
        f"- Opmerking: {form.get('notes') or '-'}\n"
        f"- Bron: {_describe_ref(form.get('ref') or 'direct')}\n"
        f"{instructions}\n"
        "Livegang: doorgaans 1–2 dagen (langer bij maatwerk).\n"
        "Facturatie: PayPal abonnement mogelijk via site; of incasso-link per e-mail na livegang.\n"
    )

    send_email_async(MAIL_TO, "Nieuwe aanvraag transfer-oplossing", body)

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
    # Bron-tracking: hidden field 'ref' meegestuurd vanuit het formulier (gevuld
    # door JS uit de URL), of fallback uit de query. Whitelist op alphanumeric/_-
    # om vrije input te voorkomen.
    raw_ref       = (request.form.get("ref") or request.args.get("ref") or "").strip()[:32]
    ref_clean     = re.sub(r"[^a-zA-Z0-9_\-]", "", raw_ref) or "direct"

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
    if len(desired_pw) < 10: errors.append("Kies een wachtwoord van minimaal 10 tekens.")

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

    # Pending account bewaren: hash het wachtwoord en sla aanvraag op.
    # Bij succesvolle PayPal-activatie wordt deze rij gebruikt om een
    # users-row aan te maken (auto-provisioning).
    pw_hash = generate_password_hash(desired_pw)
    plan_value_str = str(storage_tb_raw) if not is_more else "more"
    tenant_slug_now = current_tenant()["slug"]
    try:
        conn = db()
        try:
            # Verwijder oude awaiting_payment aanvragen van hetzelfde e-mailadres
            # in dezelfde tenant om duplicaten te voorkomen bij herhaald invullen.
            conn.execute(
                "DELETE FROM pending_accounts WHERE email = ? AND tenant_id = ? AND status = 'awaiting_payment'",
                (login_email.lower(), tenant_slug_now)
            )
            conn.execute(
                """INSERT INTO pending_accounts
                   (email, password_hash, tenant_id, plan_value, company, phone, notes, status, created_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, 'awaiting_payment', ?)""",
                (login_email.lower(), pw_hash, tenant_slug_now, plan_value_str,
                 company, phone, notes, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        log.exception("pending_account insert failed")
        # Ga verder — we willen nog steeds de mail versturen als pending insert faalt.

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
                "base_host": base_host,
                "ref": ref_clean,
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
    # Wachtwoord NIET in plaintext mailen — alleen markeren of er één is ingesteld.
    pw_marker = "ingesteld door klant" if desired_pw else "(niet ingevuld)"
    body = (
        "Er is een nieuwe aanvraag binnengekomen:\\n\\n"
        f"- Gewenste inlog-e-mail: {login_email}\\n"
        f"{storage_line}"
        f"- Bedrijfsnaam: {company}\\n"
        f"- Telefoonnummer: {phone}\\n"
        f"- Wachtwoord: {pw_marker}\\n"
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
    # Klant-email uit het contactformulier (om sub_id aan pending_account te koppelen)
    login_email = (data.get("login_email") or "").strip().lower()
    if not sub_id or plan_value not in {"0.5","1","2","5"}:
        return jsonify(ok=False, error="invalid_input"), 400
    t = current_tenant()["slug"]

    conn = db()
    try:
        # 1) Subscription bewaren (administratief; tenant-breed)
        conn.execute("""INSERT OR REPLACE INTO subscriptions(login_email, plan_value, subscription_id, status, created_at, tenant_id)
                     VALUES(?,?,?,?,?,?)""",
                  (login_email or AUTH_EMAIL, plan_value, sub_id, "ACTIVE", datetime.now(timezone.utc).isoformat(), t))

        # 2) Koppel sub_id aan pending_account zodat de webhook het account kan activeren.
        if login_email and EMAIL_RE.match(login_email):
            conn.execute(
                """UPDATE pending_accounts
                   SET paypal_subscription_id = ?, status = 'payment_started'
                   WHERE email = ? AND tenant_id = ? AND status = 'awaiting_payment'""",
                (sub_id, login_email, t)
            )
        conn.commit()
    finally:
        conn.close()

    try:
        plan_label = {"0.5":"0,5 TB","1":"1 TB","2":"2 TB","5":"5 TB"}.get(plan_value, plan_value+" TB")
        body = (
            "Er is zojuist een PayPal-abonnement gestart (via onApprove):\n\n"
            f"- Subscription ID: {sub_id}\n"
            f"- Plan: {plan_label}\n"
            f"- Klant-e-mail: {login_email or '(niet doorgegeven)'}\n"
            f"- Datum/tijd (UTC): {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            "Zodra PayPal de webhook BILLING.SUBSCRIPTION.ACTIVATED stuurt, wordt\n"
            "het account automatisch aangemaakt en kan de klant inloggen.\n"
        )
        send_email_async(MAIL_TO, "Nieuwe PayPal-abonnement gestart", body)
    except Exception:
        log.exception("Kon bevestigingsmail niet versturen")
    return jsonify(ok=True)

# -------------- PayPal Webhook --------------
def _activate_pending_account_by_sub(sub_id: str):
    """
    Activeer een pending account o.b.v. subscription_id: maak een users-row aan
    (als die nog niet bestaat) en markeer pending als 'activated'. Slaat ook
    de bedrijfsnaam op in tenant_brand zodat de UI automatisch de juiste
    branding kan tonen voor deze tenant.

    Returns: dict met 'email','tenant_id','created' of None als niet gevonden.
    """
    if not sub_id:
        return None
    conn = db()
    try:
        row = conn.execute(
            """SELECT id, email, password_hash, tenant_id, plan_value, status, company
               FROM pending_accounts
               WHERE paypal_subscription_id = ?
               LIMIT 1""",
            (sub_id,)
        ).fetchone()
        if not row:
            return None
        if row["status"] == "activated":
            # Al eerder geactiveerd (idempotent: PayPal kan webhooks re-sturen)
            return {"email": row["email"], "tenant_id": row["tenant_id"], "created": False, "already": True}

        email = (row["email"] or "").lower()
        tenant = row["tenant_id"]
        # Bestaat er al een users-row? (bijv. admin die handmatig al aanmaakte)
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ? AND tenant_id = ?",
            (email, tenant)
        ).fetchone()
        created = False
        if not existing:
            conn.execute(
                """INSERT INTO users(email, password_hash, is_admin, tenant_id, created_at, disabled)
                   VALUES(?, ?, 0, ?, ?, 0)""",
                (email, row["password_hash"], tenant, datetime.now(timezone.utc).isoformat())
            )
            created = True

        conn.execute(
            "UPDATE pending_accounts SET status = 'activated', activated_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), row["id"])
        )

        # Branding: cache de bedrijfsnaam zodat de UI 'm voor deze tenant kan tonen.
        company = (row["company"] or "").strip()
        if company and tenant:
            conn.execute(
                """INSERT INTO tenant_brand(tenant_id, company_name, updated_at)
                   VALUES(?, ?, ?)
                   ON CONFLICT(tenant_id) DO UPDATE SET
                     company_name = excluded.company_name,
                     updated_at   = excluded.updated_at""",
                (tenant, company, datetime.now(timezone.utc).isoformat())
            )

        conn.commit()
        return {"email": email, "tenant_id": tenant, "created": created, "already": False}
    except Exception:
        log.exception("activate_pending failed for sub=%s", sub_id)
        try: conn.rollback()
        except Exception: pass
        return None
    finally:
        conn.close()

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
            activation = None
            if sub_id:
                c = db()
                try:
                    # Kijk of er een pending_account aan deze sub_id gekoppeld is
                    pending = c.execute(
                        "SELECT tenant_id FROM pending_accounts WHERE paypal_subscription_id = ? LIMIT 1",
                        (sub_id,)
                    ).fetchone()
                    pending_tenant = pending["tenant_id"] if pending else current_tenant()["slug"]
                    c.execute("""INSERT OR REPLACE INTO subscriptions(login_email, plan_value, subscription_id, status, created_at, tenant_id)
                                 VALUES(?,?,?,?,?,?)""",
                              (AUTH_EMAIL, plan_value or (plan_id or ""), sub_id, status, datetime.now(timezone.utc).isoformat(), pending_tenant))
                    c.commit()
                finally:
                    c.close()
                # Probeer het pending account te activeren (maakt users-row aan)
                activation = _activate_pending_account_by_sub(sub_id)

            try:
                plan_label = {"0.5":"0,5 TB","1":"1 TB","2":"2 TB","5":"5 TB"}.get(plan_value, plan_id or "(onbekend plan)")
                if activation and activation.get("created"):
                    subject = "PayPal: abonnement + account geactiveerd"
                    body = (
                        "PayPal abonnement geactiveerd — account automatisch aangemaakt:\n\n"
                        f"- Klant-e-mail: {activation['email']}\n"
                        f"- Tenant: {activation['tenant_id']}\n"
                        f"- Subscription ID: {sub_id or '-'}\n"
                        f"- Plan: {plan_label}\n"
                        f"- Datum/tijd (UTC): {now_utc}\n\n"
                        "De klant kan direct inloggen met het wachtwoord dat hij op het\n"
                        "contactformulier heeft ingevuld.\n"
                    )
                elif activation and activation.get("already"):
                    subject = "PayPal: abonnement geactiveerd (herhaling)"
                    body = (
                        "PayPal-webhook ACTIVATED opnieuw ontvangen voor bestaand account.\n\n"
                        f"- Klant-e-mail: {activation['email']}\n"
                        f"- Subscription ID: {sub_id or '-'}\n"
                        f"- Datum/tijd (UTC): {now_utc}\n"
                    )
                elif activation and not activation.get("created"):
                    subject = "PayPal: abonnement geactiveerd (user bestond al)"
                    body = (
                        "PayPal abonnement geactiveerd. De users-row bestond al — geen nieuw account\n"
                        "aangemaakt, maar de pending_account-status is bijgewerkt.\n\n"
                        f"- Klant-e-mail: {activation['email']}\n"
                        f"- Subscription ID: {sub_id or '-'}\n"
                        f"- Plan: {plan_label}\n"
                        f"- Datum/tijd (UTC): {now_utc}\n"
                    )
                else:
                    subject = "PayPal: abonnement geactiveerd (geen pending gevonden)"
                    body = (
                        "PayPal abonnement geactiveerd, maar er is geen pending_account\n"
                        "gekoppeld aan deze subscription_id. Maak het account handmatig aan\n"
                        "via /admin/users.\n\n"
                        f"- Event: {event_type}\n"
                        f"- Subscription ID: {sub_id or '-'}\n"
                        f"- Plan: {plan_label}\n"
                        f"- Datum/tijd (UTC): {now_utc}\n"
                    )
                send_email_async(MAIL_TO, subject, body)
            except Exception:
                log.exception("Webhook mail (activated) failed")

        elif event_type in {"BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.RE-ACTIVATED"}:
            new_status = "ACTIVE" if event_type.endswith("RE-ACTIVATED") else event_type.split(".")[-1]
            if sub_id:
                c = db()
                try:
                    c.execute("UPDATE subscriptions SET status=? WHERE subscription_id=?", (new_status, sub_id))
                    c.commit()
                finally:
                    c.close()
            try:
                body = (
                    "PayPal abonnementsstatus gewijzigd:\n\n"
                    f"- Event: {event_type}\n"
                    f"- Subscription ID: {sub_id or '-'}\n"
                    f"- Plan ID: {plan_id or '-'}\n"
                    f"- Datum/tijd (UTC): {now_utc}\n"
                )
                send_email_async(MAIL_TO, f"PayPal: {event_type}", body)
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
                send_email_async(MAIL_TO, "PayPal: betaling ontvangen", body)
            except Exception:
                log.exception("Webhook mail (payment) failed")
        else:
            log.info("PayPal webhook: event %s genegeerd", event_type)

    except Exception:
        log.exception("Webhook processing error")
        return jsonify(ok=False, error="processing_error"), 500

    return jsonify(ok=True)

# Healthcheck & Aliassen
# Periodieke cleanup van oude rate-limit records via healthcheck.
# Render pingt /health elke 30s; we limiteren naar max 1x per 10 min.
_last_rate_cleanup = {"ts": 0.0}

@app.route("/health")
@app.route("/__health")
def health_basic():
    now = time.time()
    if now - _last_rate_cleanup["ts"] > 600:
        _last_rate_cleanup["ts"] = now
        # Async: healthcheck mag NOOIT blokkeren op DB. Bij een SQLite-lock
        # of trage disk zou Render anders de container als unhealthy markeren
        # en herstarten — terwijl er feitelijk niets mis is met de app.
        try:
            ex = globals().get("_bg_executor")
            if ex is not None:
                ex.submit(_rate_cleanup_periodic)
        except Exception:
            log.exception("rate cleanup submit failed")
    return {"ok": True, "service": "minitransfer", "tenant": _tenant_slug}

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


# ============================================================
# ADMIN: User management
# ============================================================
EMAIL_SIMPLE_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

ADMIN_USERS_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Gebruikersbeheer – Admin</title>{{ head_icon|safe }}
<style>{{ base_css }}
/* ===== Compacte actie-balk in tabellen ===== */
.actions-col{ width:1%; white-space:nowrap; }
.action-bar{
  display:inline-flex;
  align-items:center;
  gap:6px;
}
.action-bar form{ display:inline; margin:0; }
.icon-btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  width:30px; height:30px;
  padding:0;
  border-radius:8px;
  border:1px solid var(--line);
  background:color-mix(in oklab, var(--surface) 70%, transparent);
  color:var(--text);
  cursor:pointer;
  transition:background .15s, border-color .15s, color .15s, transform .05s;
}
.icon-btn:hover{
  background:color-mix(in oklab, var(--surface) 50%, var(--brand) 25%);
  border-color:color-mix(in oklab, var(--line) 40%, var(--brand) 60%);
  color:#fff;
}
.icon-btn:active{ transform:translateY(1px); }
.icon-btn:focus-visible{
  outline:2px solid var(--brand);
  outline-offset:2px;
}
.icon-btn.danger{ color:#ef4444; }
.icon-btn.danger:hover{
  background:color-mix(in oklab, var(--surface) 60%, #ef4444 30%);
  border-color:#b91c1c;
  color:#fff;
}
.icon-btn svg{ display:block; }

/* Reset-wachtwoord dialog */
.action-dialog{
  border:1px solid var(--line);
  border-radius:12px;
  padding:1.2rem 1.4rem;
  background:var(--surface);
  color:var(--text);
  max-width:380px;
  width:90vw;
  box-shadow:0 20px 50px rgba(0,0,0,.35);
}
.action-dialog::backdrop{
  background:rgba(0,0,0,.45);
  backdrop-filter:blur(2px);
}
.action-dialog h3{ color:var(--text); }
</style></head><body>
{{ bg|safe }}
<div class="wrap"><div class="card" style="max-width:900px;margin:auto">
  <div style="display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap">
    <h1 style="color:var(--brand);margin:0">Gebruikersbeheer</h1>
    <div>
      <a class="btn-pro secondary" href="/">← Terug</a>
      <a class="btn-pro secondary" href="/logout">Uitloggen</a>
    </div>
  </div>
  <p style="color:var(--muted)">Ingelogd als <strong>{{ me }}</strong> (admin)</p>

  {% if msg %}<div style="background:#dcfce7;color:#14532d;padding:.6rem .8rem;border-radius:10px;margin:.6rem 0">{{ msg }}</div>{% endif %}
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin:.6rem 0">{{ error }}</div>{% endif %}

  <h2 style="margin-top:1.5rem">Nieuwe gebruiker aanmaken</h2>
  <form method="post" action="/admin/users/create" style="display:grid;grid-template-columns:1fr 1fr auto;gap:.5rem;align-items:end">
    <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
    <div><label for="new_email">E-mail</label>
      <input id="new_email" class="input" type="email" name="email" required></div>
    <div><label for="new_pw">Tijdelijk wachtwoord</label>
      <input id="new_pw" class="input" type="text" name="password" minlength="8" required
             placeholder="min. 8 tekens"></div>
    <div><button class="btn-pro primary" type="submit">Aanmaken</button></div>
    <div style="grid-column:1/-1;display:flex;gap:1rem;align-items:center">
      <label style="display:flex;gap:.4rem;align-items:center">
        <input type="checkbox" name="is_admin" value="1"> Maak admin
      </label>
      <span style="color:var(--muted);font-size:.9em">
        De gebruiker kan zelf later wachtwoord wijzigen (nog niet geïmplementeerd).
      </span>
    </div>
  </form>

  <details style="margin-top:1.2rem;background:var(--surface-2);padding:.8rem 1rem;border-radius:10px;border:1px solid var(--line)">
    <summary style="cursor:pointer;font-weight:600">Aanmaken via wachtwoord-hash (uit contactmail)</summary>
    <p style="color:var(--muted);font-size:.9em;margin:.6rem 0">
      Gebruik dit als een klant zijn eigen wachtwoord heeft ingevuld op het contactformulier.
      De hash staat in de aanvraagmail onder <em>Wachtwoord-hash</em>. De klant kan zelf inloggen
      met het wachtwoord dat hij heeft ingevuld — jij weet het wachtwoord niet.
    </p>
    <form method="post" action="/admin/users/create-from-hash" style="display:grid;grid-template-columns:1fr 2fr auto;gap:.5rem;align-items:end">
      <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
      <div><label for="hash_email">E-mail</label>
        <input id="hash_email" class="input" type="email" name="email" required></div>
      <div><label for="hash_pw">Wachtwoord-hash</label>
        <input id="hash_pw" class="input" type="text" name="password_hash" required
               placeholder="pbkdf2:sha256:..."></div>
      <div><button class="btn-pro primary" type="submit">Aanmaken met hash</button></div>
      <div style="grid-column:1/-1">
        <label style="display:flex;gap:.4rem;align-items:center">
          <input type="checkbox" name="is_admin" value="1"> Maak admin
        </label>
      </div>
    </form>
  </details>

  <h2 style="margin-top:2rem">Bestaande gebruikers</h2>
  <table class="table">
    <thead><tr>
      <th>E-mail</th><th>Rol</th><th>Status</th><th>Aangemaakt</th>
      {% if show_tenant_col %}<th>Tenant</th>{% endif %}
      <th class="actions-col">Acties</th>
    </tr></thead>
    <tbody>
      {% for u in users %}
      <tr>
        <td>{{ u.email }}{% if u.id == my_id %} <em style="color:var(--muted)">(jij)</em>{% endif %}</td>
        <td>{{ 'Admin' if u.is_admin else 'Gebruiker' }}</td>
        <td>{{ 'Uitgeschakeld' if u.disabled else 'Actief' }}</td>
        <td style="font-size:.85em;color:var(--muted)">{{ u.created_at[:10] }}</td>
        {% if show_tenant_col %}
        <td style="font-size:.85em;color:var(--muted)"><code>{{ u.tenant_id }}</code></td>
        {% endif %}
        <td class="actions-col">
          {% if u.id != my_id %}
          <div class="action-bar">
            <form method="post" action="/admin/users/{{ u.id }}/toggle">
              <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
              <button class="icon-btn"
                      type="submit"
                      title="{{ 'Account aanzetten' if u.disabled else 'Account uitschakelen' }}"
                      aria-label="{{ 'Account aanzetten' if u.disabled else 'Account uitschakelen' }}">
                {% if u.disabled %}
                  {# play / activate #}
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="6 4 20 12 6 20 6 4"/></svg>
                {% else %}
                  {# pause / disable #}
                  <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
                {% endif %}
              </button>
            </form>
            <button class="icon-btn"
                    type="button"
                    title="Wachtwoord resetten"
                    aria-label="Wachtwoord resetten"
                    onclick="document.getElementById('reset-{{ u.id }}').showModal()">
              {# key #}
              <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="4.5"/><path d="m10.5 12.5 9.5-9.5"/><path d="m17 6 3 3"/><path d="m20 3 1 1"/></svg>
            </button>
            <form method="post" action="/admin/users/{{ u.id }}/delete"
                  onsubmit="return confirm('Zeker weten dat je {{ u.email }} wilt verwijderen? Bestanden van deze gebruiker blijven bestaan.');">
              <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
              <button class="icon-btn danger"
                      type="submit"
                      title="Gebruiker verwijderen"
                      aria-label="Gebruiker verwijderen">
                {# trash #}
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
              </button>
            </form>
          </div>

          <dialog id="reset-{{ u.id }}" class="action-dialog">
            <form method="post" action="/admin/users/{{ u.id }}/reset">
              <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
              <h3 style="margin:0 0 .4rem">Wachtwoord resetten</h3>
              <p style="margin:0 0 1rem;color:var(--muted);font-size:.9em">
                Voor <strong>{{ u.email }}</strong>
              </p>
              <label for="pw-{{ u.id }}" style="display:block;margin-bottom:.3rem;font-size:.85em">Nieuw wachtwoord</label>
              <input id="pw-{{ u.id }}" class="input" type="text" name="password"
                     minlength="8" required placeholder="min. 8 tekens"
                     style="width:100%;margin-bottom:1rem">
              <div style="display:flex;gap:.5rem;justify-content:flex-end">
                <button class="btn-pro secondary sm" type="button"
                        onclick="this.closest('dialog').close()">Annuleren</button>
                <button class="btn-pro primary sm" type="submit">Reset wachtwoord</button>
              </div>
            </form>
          </dialog>
          {% else %}
          <em style="color:var(--muted)">eigen account</em>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>

  {% if pending_accounts %}
  <h2 style="margin-top:2rem">Aanvragen in behandeling</h2>
  <p style="color:var(--muted);font-size:.9em">
    Accounts die via het contactformulier zijn aangevraagd. Zodra de PayPal-betaling
    binnenkomt (webhook <code>BILLING.SUBSCRIPTION.ACTIVATED</code>), wordt het account
    automatisch aangemaakt en verschijnt het in de lijst hierboven.
  </p>
  <table class="table">
    <thead><tr>
      <th>E-mail</th><th>Bedrijf</th><th>Plan</th><th>Status</th><th>Aangevraagd</th><th>Sub ID</th>
      {% if show_tenant_col %}<th>Tenant</th>{% endif %}
      <th class="actions-col">Acties</th>
    </tr></thead>
    <tbody>
      {% for p in pending_accounts %}
      <tr>
        <td>{{ p.email }}</td>
        <td>{{ p.company or '-' }}</td>
        <td>{{ p.plan_value or '-' }}</td>
        <td>
          {% if p.status == 'awaiting_payment' %}
            <span style="color:#92400e">Wacht op betaling</span>
          {% elif p.status == 'payment_started' %}
            <span style="color:#1e40af">Betaling gestart</span>
          {% elif p.status == 'activated' %}
            <span style="color:#166534">Geactiveerd</span>
          {% elif p.status == 'cancelled' %}
            <span style="color:#991b1b">Geannuleerd</span>
          {% else %}{{ p.status }}{% endif %}
        </td>
        <td style="font-size:.85em;color:var(--muted)">{{ p.created_at[:16] }}</td>
        <td style="font-size:.75em;color:var(--muted)"><code>{{ p.paypal_subscription_id or '-' }}</code></td>
        {% if show_tenant_col %}
        <td style="font-size:.85em;color:var(--muted)"><code>{{ p.tenant_id }}</code></td>
        {% endif %}
        <td class="actions-col">
          {% if p.status in ('awaiting_payment', 'payment_started') %}
          <div class="action-bar">
            <form method="post" action="/admin/pending/{{ p.id }}/cancel"
                  onsubmit="return confirm('Aanvraag voor {{ p.email }} annuleren? Deze rij wordt verwijderd. Als de klant later toch betaalt, wordt automatisch een nieuwe pending aangemaakt via de webhook.');">
              <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
              <button class="icon-btn danger"
                      type="submit"
                      title="Aanvraag annuleren"
                      aria-label="Aanvraag annuleren">
                {# x-circle #}
                <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>
              </button>
            </form>
          </div>
          {% else %}
          <em style="color:var(--muted)">-</em>
          {% endif %}
        </td>
      </tr>
      {% endfor %}
    </tbody>
  </table>
  {% endif %}
</div></div></body></html>
"""

def _require_admin():
    if not logged_in():
        abort(401)
    if not is_admin():
        abort(403)

def _flash(msg=None, error=None):
    """Simpele flash via session (one-shot)."""
    if msg:
        session["_flash_msg"] = msg
    if error:
        session["_flash_err"] = error

def _pop_flash():
    return session.pop("_flash_msg", None), session.pop("_flash_err", None)

@app.route("/admin/users")
def admin_users():
    _require_admin()
    me = current_user()
    # Root-admin (AUTH_EMAIL) ziet álle users en pending accounts over álle tenants.
    # Dit voorkomt dat accounts die ooit op een andere tenant_id zijn beland
    # (bv. via de trial-tenant) onzichtbaar blijven, terwijl de duplicaat-check
    # in /admin/users/create wél globaal is en dus klaagt dat ze "bestaan".
    is_root_admin = (me["email"].lower() == AUTH_EMAIL)
    conn = db()
    try:
        if is_root_admin:
            rows = conn.execute(
                "SELECT id, email, is_admin, disabled, created_at, tenant_id "
                "FROM users ORDER BY created_at ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email, is_admin, disabled, created_at, tenant_id "
                "FROM users WHERE tenant_id = ? ORDER BY created_at ASC",
                (me["tenant_id"],)
            ).fetchall()
        # Toon pending accounts van laatste 30 dagen (afgehandeld en nog-open)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        if is_root_admin:
            pending_rows = conn.execute(
                """SELECT id, email, company, plan_value, status, created_at,
                          paypal_subscription_id, tenant_id
                   FROM pending_accounts
                   WHERE created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT 100""",
                (cutoff,)
            ).fetchall()
        else:
            pending_rows = conn.execute(
                """SELECT id, email, company, plan_value, status, created_at,
                          paypal_subscription_id, tenant_id
                   FROM pending_accounts
                   WHERE tenant_id = ? AND created_at >= ?
                   ORDER BY created_at DESC
                   LIMIT 100""",
                (me["tenant_id"], cutoff)
            ).fetchall()
    finally:
        conn.close()
    msg, err = _pop_flash()
    return render_template_string(
        ADMIN_USERS_HTML,
        users=[dict(r) for r in rows],
        pending_accounts=[dict(r) for r in pending_rows],
        me=me["email"],
        my_id=me["id"],
        show_tenant_col=is_root_admin,
        msg=msg, error=err,
        base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON
    )

@app.route("/admin/users/create", methods=["POST"])
def admin_users_create():
    _require_admin()
    me = current_user()
    email = (request.form.get("email") or "").strip().lower()
    pw = (request.form.get("password") or "").strip()
    mk_admin = (request.form.get("is_admin") == "1")

    if not EMAIL_SIMPLE_RE.match(email):
        _flash(error="Ongeldig e-mailadres.")
        return redirect(url_for("admin_users"))
    if len(pw) < 8:
        _flash(error="Wachtwoord moet minimaal 8 tekens zijn.")
        return redirect(url_for("admin_users"))

    conn = db()
    try:
        # Consistent met admin_users(): root-admin checkt globaal, anderen
        # binnen hun eigen tenant. Zo wordt geen account geblokkeerd door een
        # onzichtbare collision in een andere tenant.
        if me["email"].lower() == AUTH_EMAIL:
            exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        else:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE email = ? AND tenant_id = ?",
                (email, me["tenant_id"])
            ).fetchone()
        if exists:
            _flash(error=f"Gebruiker {email} bestaat al.")
            return redirect(url_for("admin_users"))
        conn.execute(
            "INSERT INTO users(email, password_hash, is_admin, tenant_id, created_at, disabled) VALUES(?,?,?,?,?,0)",
            (email, generate_password_hash(pw), 1 if mk_admin else 0, me["tenant_id"], datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        _flash(msg=f"Gebruiker {email} aangemaakt.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/users/create-from-hash", methods=["POST"])
def admin_users_create_from_hash():
    """
    Account aanmaken via een reeds gehashed wachtwoord. Bedoeld voor de flow
    waarbij een klant op /contact zijn eigen wachtwoord invult: de server hasht
    het meteen en mailt de hash naar de admin. De admin plakt hier de hash,
    zonder ooit het plaintext wachtwoord te kennen.
    """
    _require_admin()
    me = current_user()
    email = (request.form.get("email") or "").strip().lower()
    pw_hash = (request.form.get("password_hash") or "").strip()
    mk_admin = (request.form.get("is_admin") == "1")

    if not EMAIL_SIMPLE_RE.match(email):
        _flash(error="Ongeldig e-mailadres.")
        return redirect(url_for("admin_users"))

    # Basis-validatie op hash-format. Werkzeug-hashes beginnen met het scheme,
    # bijv. 'pbkdf2:sha256:600000$salt$hexdigest' of 'scrypt:...'.
    # We accepteren alles wat lijkt op een werkzeug hash: bevat ':' en '$'.
    if not pw_hash or ":" not in pw_hash or "$" not in pw_hash or len(pw_hash) < 40:
        _flash(error="Ongeldige wachtwoord-hash. Verwacht een werkzeug pbkdf2/scrypt hash.")
        return redirect(url_for("admin_users"))

    conn = db()
    try:
        # Consistent met admin_users(): root-admin checkt globaal, anderen
        # binnen hun eigen tenant.
        if me["email"].lower() == AUTH_EMAIL:
            exists = conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone()
        else:
            exists = conn.execute(
                "SELECT 1 FROM users WHERE email = ? AND tenant_id = ?",
                (email, me["tenant_id"])
            ).fetchone()
        if exists:
            _flash(error=f"Gebruiker {email} bestaat al.")
            return redirect(url_for("admin_users"))
        conn.execute(
            "INSERT INTO users(email, password_hash, is_admin, tenant_id, created_at, disabled) VALUES(?,?,?,?,?,0)",
            (email, pw_hash, 1 if mk_admin else 0, me["tenant_id"], datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        _flash(msg=f"Gebruiker {email} aangemaakt via hash. Klant kan inloggen met het wachtwoord van het contactformulier.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
def admin_users_toggle(user_id):
    _require_admin()
    me = current_user()
    if user_id == me["id"]:
        _flash(error="Je kunt je eigen account niet uitschakelen.")
        return redirect(url_for("admin_users"))
    is_root_admin = (me["email"].lower() == AUTH_EMAIL)
    conn = db()
    try:
        if is_root_admin:
            row = conn.execute("SELECT disabled FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT disabled FROM users WHERE id = ? AND tenant_id = ?", (user_id, me["tenant_id"])).fetchone()
        if not row:
            abort(404)
        new_val = 0 if row["disabled"] else 1
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (new_val, user_id))
        conn.commit()
        _flash(msg="Status gewijzigd.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/reset", methods=["POST"])
def admin_users_reset(user_id):
    _require_admin()
    me = current_user()
    pw = (request.form.get("password") or "").strip()
    if len(pw) < 8:
        _flash(error="Wachtwoord moet minimaal 8 tekens zijn.")
        return redirect(url_for("admin_users"))
    is_root_admin = (me["email"].lower() == AUTH_EMAIL)
    conn = db()
    try:
        if is_root_admin:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM users WHERE id = ? AND tenant_id = ?", (user_id, me["tenant_id"])).fetchone()
        if not row:
            abort(404)
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(pw), user_id))
        conn.commit()
        _flash(msg="Wachtwoord gereset.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def admin_users_delete(user_id):
    _require_admin()
    me = current_user()
    if user_id == me["id"]:
        _flash(error="Je kunt je eigen account niet verwijderen.")
        return redirect(url_for("admin_users"))
    is_root_admin = (me["email"].lower() == AUTH_EMAIL)
    conn = db()
    try:
        if is_root_admin:
            row = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute("SELECT id FROM users WHERE id = ? AND tenant_id = ?", (user_id, me["tenant_id"])).fetchone()
        if not row:
            abort(404)
        # We verwijderen alleen de user; packages blijven bestaan (owner_user_id wordt wees)
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        _flash(msg="Gebruiker verwijderd.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/pending/<int:pending_id>/cancel", methods=["POST"])
def admin_pending_cancel(pending_id):
    """
    Annuleer een pending account-aanvraag (verwijdert de rij uit pending_accounts).
    Alleen toegestaan voor 'awaiting_payment' en 'payment_started'; reeds geactiveerde
    aanvragen mogen hier niet weg, want daar hangt al een users-row aan vast.

    Tenant-scoping: een gewone admin kan alleen aanvragen uit zijn eigen tenant
    annuleren. De root-admin (AUTH_EMAIL) mag aanvragen uit alle tenants annuleren.
    """
    _require_admin()
    me = current_user()
    is_root_admin = (me["email"].lower() == AUTH_EMAIL)
    conn = db()
    try:
        if is_root_admin:
            row = conn.execute(
                "SELECT id, email, status FROM pending_accounts WHERE id = ?",
                (pending_id,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id, email, status FROM pending_accounts WHERE id = ? AND tenant_id = ?",
                (pending_id, me["tenant_id"])
            ).fetchone()
        if not row:
            abort(404)
        if row["status"] == "activated":
            _flash(error="Deze aanvraag is al geactiveerd en kan niet meer geannuleerd worden.")
            return redirect(url_for("admin_users"))
        conn.execute("DELETE FROM pending_accounts WHERE id = ?", (pending_id,))
        conn.commit()
        _flash(msg=f"Aanvraag voor {row['email']} geannuleerd.")
    finally:
        conn.close()
    return redirect(url_for("admin_users"))


# ============================================================
# MY UPLOADS: gebruiker ziet/beheert eigen uploads
# ============================================================

MY_UPLOADS_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Mijn uploads · {{ brand.name }}</title>{{ head_icon|safe }}
<style>
{{ base_css }}
:root{
  --oh-surface:rgba(255,255,255,.82);
  --oh-surface-2:rgba(248,250,252,.70);
  --oh-border:rgba(148,163,184,.35);
  --oh-border-strong:rgba(148,163,184,.55);
  --oh-text:#f8fafc;
  --oh-muted:#a8b1c0;
  --oh-brand:#8ab4ff;
  --oh-brand-2:#b7d0ff;
  --oh-accent:#d97706;
  --oh-success:#16a34a;
  --oh-danger:#dc2626;
  --oh-radius:12px;
  --oh-radius-sm:10px;
  --oh-shadow:0 8px 30px rgba(15,23,42,.22);
}

@media (prefers-color-scheme: dark){
  :root{
    --oh-surface:rgba(17,24,39,.78);
    --oh-surface-2:rgba(15,23,42,.52);
    --oh-border:rgba(148,163,184,.18);
    --oh-border-strong:rgba(148,163,184,.28);
    --oh-text:#f8fafc;
    --oh-muted:#a8b1c0;
    --oh-brand:#8ab4ff;
    --oh-brand-2:#b7d0ff;
    --oh-shadow:0 8px 30px rgba(0,0,0,.35);
  }
}

*,*::before,*::after{box-sizing:border-box}

html,body{
  min-height:100%;
  background:transparent;
  color:var(--oh-text);
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Inter",Roboto,sans-serif;
  font-size:15px;
  line-height:1.5;
  margin:0;
}

.oh-shell{
  position:relative;
  z-index:1;
  max-width:1100px;
  margin:0 auto;
  padding:24px 18px 48px;
}

/* Topbar */
.oh-topbar{
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:20px;
  padding:14px 18px;
  background:rgba(30,41,59,.78);
  backdrop-filter:blur(10px) saturate(1.05);
  -webkit-backdrop-filter:blur(10px) saturate(1.05);
  border:1px solid rgba(255,255,255,.10);
  border-radius:12px;
  box-shadow:var(--oh-shadow);
  margin-bottom:18px;
  flex-wrap:wrap;
}

.oh-brand{
  display:flex;
  align-items:center;
  gap:12px;
}

.oh-brand-mark{
  width:38px;
  height:38px;
  border-radius:10px;
  background:linear-gradient(135deg,#7aa7ff,#5b86f7);
  display:grid;
  place-items:center;
  color:#fff;
  font-weight:700;
  font-size:14px;
  box-shadow:inset 0 -2px 0 rgba(0,0,0,.15);
  overflow:hidden;
}
.oh-brand-mark svg{width:100%;height:100%;display:block}

.oh-brand-text h1{
  margin:0;
  font-size:17px;
  font-weight:700;
  color:#ffffff;
}

.oh-brand-text p{
  margin:0;
  font-size:12px;
  color:rgba(255,255,255,.72);
}

.oh-userbar{
  display:flex;
  align-items:center;
  gap:12px;
  font-size:13px;
  color:rgba(255,255,255,.95);
  flex-wrap:wrap;
}

.oh-userbar strong{
  color:#ffffff;
  font-weight:700;
}

.oh-userbar a{
  color:#f8fafc;
  text-decoration:none;
  padding:7px 12px;
  border-radius:var(--oh-radius-sm);
  background:rgba(255,255,255,.10);
  border:1px solid rgba(255,255,255,.20);
  transition:background .15s, color .15s, border-color .15s;
}

.oh-userbar a:hover{
  background:rgba(255,255,255,.18);
  color:#ffffff;
  border-color:rgba(255,255,255,.32);
}

/* Cards */
.oh-card{
  background:rgba(30,41,59,.72);
  backdrop-filter:blur(10px) saturate(1.05);
  -webkit-backdrop-filter:blur(10px) saturate(1.05);
  border:1px solid rgba(255,255,255,.10);
  border-radius:14px;
  box-shadow:var(--oh-shadow);
  overflow:hidden;
}

.oh-card-head{
  padding:16px 18px;
  border-bottom:1px solid rgba(255,255,255,.08);
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:16px;
  background:rgba(15,23,42,.28);
  flex-wrap:wrap;
}

.oh-card-head h2{
  margin:0;
  font-size:15px;
  font-weight:700;
  display:flex;
  align-items:center;
  gap:10px;
  color:#ffffff;
}

.oh-card-head h2 svg{
  color:var(--oh-brand);
}

.oh-card-body{
  padding:18px;
}

/* Buttons */
.oh-btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:8px;
  padding:8px 12px;
  background:#8ab4ff;
  color:#0f172a;
  border:none;
  border-radius:10px;
  font-size:13px;
  font-weight:700;
  cursor:pointer;
  text-decoration:none;
  transition:background .15s, transform .05s, border-color .15s;
}

.oh-btn:hover{
  background:#b7d0ff;
}

.oh-btn.ghost{
  background:rgba(15,23,42,.42);
  color:#dbeafe;
  border:1px solid rgba(255,255,255,.08);
}

.oh-btn.ghost:hover{
  background:rgba(15,23,42,.60);
  color:#ffffff;
  border-color:rgba(255,255,255,.14);
}

.oh-btn.danger{
  background:#ffffff;
  color:var(--oh-danger);
  border:1px solid #fecaca;
}

.oh-btn.danger:hover{
  background:#fff5f5;
  border-color:#fca5a5;
}

.oh-btn.xs{
  min-width:0;
  padding:4px 8px;
  font-size:11px;
  font-weight:700;
  border-radius:8px;
  gap:4px;
}

/* Compacte icon-knoppen voor de actiekolom */
.oh-icon-btn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  gap:4px;
  width:28px; height:28px;
  padding:0;
  border-radius:8px;
  background:rgba(15,23,42,.42);
  color:#dbeafe;
  border:1px solid rgba(255,255,255,.08);
  cursor:pointer;
  text-decoration:none;
  transition:background .15s, border-color .15s, color .15s, transform .05s;
}
.oh-icon-btn:hover{
  background:rgba(15,23,42,.65);
  color:#fff;
  border-color:rgba(255,255,255,.18);
}
.oh-icon-btn:active{ transform:translateY(1px); }
.oh-icon-btn:focus-visible{
  outline:2px solid var(--oh-brand, #5b86f7);
  outline-offset:2px;
}
.oh-icon-btn.wide{
  width:auto;
  padding:0 8px;
  font-size:11px;
  font-weight:700;
}
.oh-icon-btn.danger{
  color:#fca5a5;
  border-color:rgba(248,113,113,.25);
}
.oh-icon-btn.danger:hover{
  background:color-mix(in oklab, rgba(15,23,42,.5) 50%, #b91c1c 50%);
  color:#fff;
  border-color:#b91c1c;
}
.oh-icon-btn svg{ display:block; }

/* Flash */
.oh-flash{
  padding:10px 14px;
  border-radius:10px;
  margin-bottom:16px;
  font-size:14px;
}

.oh-flash.ok{
  background:#dcfce7;
  color:#14532d;
  border:1px solid #bbf7d0;
}

.oh-flash.err{
  background:#fee2e2;
  color:#991b1b;
  border:1px solid #fecaca;
}

/* Empty state */
.oh-empty{
  padding:60px 20px;
  text-align:center;
  color:#d1d5db;
}

.oh-empty svg{
  margin-bottom:12px;
  color:#94a3b8;
}

.oh-empty p{
  margin:0 0 14px 0;
}

/* Summary */
.oh-summary{
  display:grid;
  grid-template-columns:repeat(4,minmax(120px,1fr));
  gap:12px;
  padding:14px;
  background:rgba(15,23,42,.24);
  border:1px solid rgba(255,255,255,.08);
  border-radius:10px;
  margin-bottom:14px;
}

.oh-summary > div{
  padding:10px 12px;
  background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.05);
  border-radius:10px;
}

.oh-summary .k{
  font-size:11px;
  color:var(--oh-muted);
  text-transform:uppercase;
  letter-spacing:.05em;
  font-weight:700;
  margin-bottom:4px;
}

.oh-summary .v{
  font-size:24px;
  font-weight:700;
  color:#ffffff;
  font-variant-numeric:tabular-nums;
}

/* Filters */
.oh-filters{
  display:flex;
  gap:8px;
  align-items:center;
  flex-wrap:wrap;
  margin-bottom:14px;
}

.oh-filter-btn{
  padding:7px 12px;
  border-radius:10px;
  background:rgba(15,23,42,.24);
  border:1px solid rgba(255,255,255,.08);
  color:#e5e7eb;
  font-size:13px;
  cursor:pointer;
  text-decoration:none;
}

.oh-filter-btn.active{
  background:#8ab4ff;
  color:#0f172a;
  border-color:#8ab4ff;
  font-weight:700;
}

.oh-filter-btn:hover{
  border-color:rgba(255,255,255,.20);
}

/* Sorteerbare tabelkoppen — klik op de kolomtitel om te sorteren */
.oh-table thead th.sortable{
  cursor:pointer;
  user-select:none;
  transition:color .15s, background .15s;
  border-radius:6px;
}
.oh-table thead th.sortable .oh-th-label{
  display:inline-block;
}
.oh-table thead th.sortable .arr{
  display:inline-block;
  margin-left:4px;
  font-size:10px;
  opacity:.6;
  min-width:8px;
}
.oh-table thead th.sortable:hover{
  color:#fff;
  background:rgba(255,255,255,.04);
}
.oh-table thead th.sortable:focus-visible{
  outline:2px solid #8ab4ff;
  outline-offset:2px;
  color:#fff;
}
.oh-table thead th.sortable.active{
  color:#8ab4ff;
}
.oh-table thead th.sortable.active .arr{
  opacity:1;
}

/* Mobile sort UI — alleen zichtbaar op smalle schermen waar de tabelkoppen
   zelf verborgen worden (<720px in de mobile-stack-layout hieronder). */
.oh-mobile-sort{
  display:none;
  align-items:center;
  gap:8px;
  margin-bottom:12px;
  padding:8px 10px;
  background:rgba(15,23,42,.24);
  border:1px solid rgba(255,255,255,.08);
  border-radius:10px;
}
.oh-mobile-sort-label{
  font-size:11px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.05em;
  color:var(--oh-muted);
  white-space:nowrap;
}
.oh-mobile-sort-select{
  flex:1;
  min-width:0;
  padding:6px 10px;
  background:rgba(15,23,42,.42);
  color:#dbeafe;
  border:1px solid rgba(255,255,255,.10);
  border-radius:8px;
  font-size:13px;
  font-weight:600;
  cursor:pointer;
}
.oh-mobile-sort-select:focus-visible{
  outline:2px solid #8ab4ff;
  outline-offset:1px;
}
.oh-mobile-sort-dir{
  width:32px; height:32px;
  display:inline-flex;
  align-items:center;
  justify-content:center;
  padding:0;
  border-radius:8px;
  background:rgba(15,23,42,.42);
  color:#dbeafe;
  border:1px solid rgba(255,255,255,.10);
  cursor:pointer;
  font-size:14px;
  transition:background .15s, color .15s;
}
.oh-mobile-sort-dir:hover{
  background:rgba(15,23,42,.65);
  color:#fff;
}
.oh-mobile-sort-dir:focus-visible{
  outline:2px solid #8ab4ff;
  outline-offset:1px;
}

/* Table */
.oh-table{
  width:100%;
  border-collapse:separate;
  border-spacing:0 6px;
}

.oh-table thead th{
  text-align:left;
  padding:0 10px 6px 10px;
  font-size:10px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.05em;
  color:#a8b1c0;
  border:0;
  background:transparent;
}

.oh-table tbody tr{
  background:rgba(255,255,255,.03);
}

.oh-table tbody td{
  padding:8px 10px;
  border-top:1px solid rgba(255,255,255,.06);
  border-bottom:1px solid rgba(255,255,255,.06);
  font-size:13px;
  vertical-align:middle;
  color:#f8fafc;
}

.oh-table tbody td:first-child{
  border-left:1px solid rgba(255,255,255,.06);
  border-radius:10px 0 0 10px;
}

.oh-table tbody td:last-child{
  border-right:1px solid rgba(255,255,255,.06);
  border-radius:0 10px 10px 0;
}

.oh-table tbody tr:hover td{
  background:rgba(255,255,255,.05);
}

.oh-title-cell{
  display:flex;
  flex-direction:column;
  gap:0;
  line-height:1.2;
}

.oh-title-cell strong{
  font-size:13px;
  line-height:1.2;
  color:#f8fafc;
  font-weight:600;
}

.oh-title-cell .tok{
  margin-top:1px;
  font-family:ui-monospace,"SF Mono",Menlo,monospace;
  font-size:10px;
  color:#94a3b8;
}

.oh-stat-cell{
  color:#cbd5e1;
  font-size:12px;
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
}

/* Badges */
.oh-badge{
  display:inline-block;
  padding:2px 7px;
  border-radius:999px;
  font-size:10px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:.03em;
}

.oh-badge.ok{
  background:#dcfce7;
  color:#14532d;
}

.oh-badge.warn{
  background:#fef3c7;
  color:#854d0e;
}

.oh-badge.exp{
  background:#fee2e2;
  color:#991b1b;
}

.oh-badge.pw{
  background:#dbeafe;
  color:#1e3a8a;
  margin-left:4px;
}

.oh-download-pill{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  min-width:24px;
  height:22px;
  padding:0 8px;
  border-radius:999px;
  background:#27324a;
  border:1px solid rgba(255,255,255,.06);
  color:#f8fafc;
  font-weight:700;
  font-variant-numeric:tabular-nums;
  font-size:11px;
}

/* Actions - horizontaal, compact */
.oh-actions{
  display:flex;
  flex-direction:row;
  gap:4px;
  align-items:center;
  justify-content:flex-end;
  flex-wrap:wrap;
}

/* Mobile */
@media (max-width: 900px){
  .oh-summary{
    grid-template-columns:repeat(2,minmax(120px,1fr));
  }
}

@media (max-width: 720px){
  .oh-mobile-sort{ display:flex; }
  .oh-table thead{display:none}
  .oh-table,.oh-table tbody,.oh-table tr,.oh-table td{display:block;width:100%}
  .oh-table{border-spacing:0}
  .oh-table tr{
    margin-bottom:12px;
    border:1px solid rgba(255,255,255,.08);
    border-radius:12px;
    padding:10px;
    background:rgba(255,255,255,.03);
  }
  .oh-table td{
    border:0;
    padding:6px 4px;
    border-radius:0 !important;
  }
  .oh-table td::before{
    content:attr(data-label);
    display:block;
    font-size:11px;
    font-weight:700;
    text-transform:uppercase;
    color:var(--oh-muted);
    margin-bottom:2px;
  }
  .oh-actions{
    align-items:flex-start;
    margin-top:8px;
  }
}
</style></head><body>
{{ bg|safe }}

<div class="oh-shell">
  <header class="oh-topbar">
    <div class="oh-brand">
      <div class="oh-brand-mark"{% if brand.color %} style="background:{{ brand.color }}"{% endif %}>
        {% if brand.logo_svg %}{{ brand.logo_svg|safe }}{% else %}{{ brand.short }}{% endif %}
      </div>
      <div class="oh-brand-text">
        <h1>{{ brand.name }}</h1>
        <p>Mijn uploads</p>
      </div>
    </div>
    <div class="oh-userbar">
      <span>Ingelogd als <strong>{{ user }}</strong></span>
      <a href="/">← Uploaden</a>
      {% if is_admin %}<a href="/admin/users">Beheer</a>{% endif %}
      <a href="{{ url_for('logout') }}">Uitloggen</a>
    </div>
  </header>

  {% if flash_msg %}<div class="oh-flash ok">{{ flash_msg }}</div>{% endif %}
  {% if flash_err %}<div class="oh-flash err">{{ flash_err }}</div>{% endif %}

  <section class="oh-card">
    <div class="oh-card-head">
      <h2>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        {% if show_all %}Alle uploads (admin){% else %}Mijn uploads{% endif %}
      </h2>
      <a class="oh-btn" href="/">+ Nieuwe upload</a>
    </div>
    <div class="oh-card-body">

      <div class="oh-summary">
        <div><div class="k">Totaal pakketten</div><div class="v">{{ summary.pkg_count }}</div></div>
        <div><div class="k">Actief</div><div class="v">{{ summary.active }}</div></div>
        <div><div class="k">Verlopen</div><div class="v">{{ summary.expired }}</div></div>
        <div><div class="k">Totale grootte</div><div class="v">{{ summary.total_human }}</div></div>
      </div>

      {% if is_admin %}
      <div class="oh-filters">
        <a class="oh-filter-btn {% if not show_all %}active{% endif %}" href="?scope=mine">Alleen mijn</a>
        <a class="oh-filter-btn {% if show_all %}active{% endif %}" href="?scope=all">Alle gebruikers</a>
      </div>
      {% endif %}

      {% if not packages %}
      <div class="oh-empty">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <p>Nog geen uploads. Klik hieronder om je eerste bestand te delen.</p>
        <a class="oh-btn" href="/">Bestand uploaden</a>
      </div>
      {% else %}
      <div class="oh-mobile-sort" id="ohMobileSort">
        <label class="oh-mobile-sort-label" for="ohMobileSortField">Sorteer op</label>
        <select id="ohMobileSortField" class="oh-mobile-sort-select">
          <option value="created" selected>Aangemaakt</option>
          <option value="expires">Verloopt</option>
          <option value="size">Grootte</option>
          <option value="downloads">Downloads</option>
          <option value="title">Onderwerp</option>
          {% if show_all %}<option value="owner">Eigenaar</option>{% endif %}
        </select>
        <button type="button" id="ohMobileSortDir" class="oh-mobile-sort-dir"
                data-dir="desc" title="Richting wisselen" aria-label="Richting wisselen">
          <span class="arr">▼</span>
        </button>
      </div>
      <table class="oh-table" id="ohTable">
        <thead>
          <tr>
            <th class="sortable" data-sort="title" data-dir="asc">
              <span class="oh-th-label">Onderwerp / token</span><span class="arr"></span>
            </th>
            {% if show_all %}
            <th class="sortable" data-sort="owner" data-dir="asc">
              <span class="oh-th-label">Eigenaar</span><span class="arr"></span>
            </th>
            {% endif %}
            <th>Bestanden</th>
            <th class="sortable" data-sort="size" data-dir="desc">
              <span class="oh-th-label">Grootte</span><span class="arr"></span>
            </th>
            <th class="sortable active" data-sort="created" data-dir="desc">
              <span class="oh-th-label">Aangemaakt</span><span class="arr">▼</span>
            </th>
            <th class="sortable" data-sort="expires" data-dir="asc">
              <span class="oh-th-label">Verloopt</span><span class="arr"></span>
            </th>
            <th>Status</th>
            <th class="sortable" data-sort="downloads" data-dir="desc">
              <span class="oh-th-label">Downloads</span><span class="arr"></span>
            </th>
            <th>Laatst gedownload</th>
            <th style="text-align:right">Acties</th>
          </tr>
        </thead>
        <tbody id="ohTableBody">
          {% for p in packages %}
          <tr
            data-created="{{ p.created_at }}"
            data-expires="{{ p.expires_at }}"
            data-size="{{ p.size_bytes }}"
            data-downloads="{{ p.download_count }}"
            data-title="{{ (p.title or '') | lower }}"
            data-owner="{{ (p.owner_email or '') | lower }}"
          >
            <td data-label="Onderwerp"><div class="oh-title-cell">
              <strong>{{ p.title or '(geen titel)' }}</strong>
              <span class="tok">{{ p.token }}</span>
            </div></td>
            {% if show_all %}<td data-label="Eigenaar" class="oh-stat-cell">{{ p.owner_email or '(wees)' }}</td>{% endif %}
            <td data-label="Bestanden" class="oh-stat-cell">{{ p.item_count }}</td>
            <td data-label="Grootte" class="oh-stat-cell">{{ p.size_human }}</td>
            <td data-label="Aangemaakt" class="oh-stat-cell">{{ p.created_at[:10] }}</td>
            <td data-label="Verloopt" class="oh-stat-cell">{% if p.is_never %}—{% else %}{{ p.expires_at[:10] }}{% endif %}</td>
            <td data-label="Status">
              {% if p.is_expired %}
                <span class="oh-badge exp">Verlopen</span>
              {% elif p.is_never %}
                <span class="oh-badge ok">Onbeperkt</span>
              {% elif p.days_left <= 3 %}
                <span class="oh-badge warn">Nog {{ p.days_left }}d</span>
              {% else %}
                <span class="oh-badge ok">Nog {{ p.days_left }}d</span>
              {% endif %}
              {% if p.has_password %}<span class="oh-badge pw">PW</span>{% endif %}
            </td>
            <td data-label="Downloads">
            <span class="oh-download-pill">{{ p.download_count }}</span>
            </td>
            <td data-label="Laatst gedownload" class="oh-stat-cell">{{ p.last_download_at }}</td>
            <td data-label="Acties">
              <div class="oh-actions">
                {% if not p.is_expired %}
                <a class="oh-icon-btn" href="/p/{{ p.token }}" target="_blank" rel="noopener"
                   title="Pakket openen" aria-label="Pakket openen">
                  {# external-link #}
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                </a>
                <button class="oh-icon-btn" type="button"
                        data-copy-token="{{ p.token }}"
                        data-needs-unlock="{{ '1' if p.needs_unlock else '0' }}"
                        {% if not p.needs_unlock %}data-share-link="{{ p.share_link }}"{% endif %}
                        onclick="copyLink(this)"
                        title="Link kopiëren" aria-label="Link kopiëren">
                  {# copy / link #}
                  <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                </button>
                {% if not p.is_never %}
                <form method="post" action="/uploads/{{ p.token }}/extend" style="display:inline">
                  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
                  <button class="oh-icon-btn wide" type="submit"
                          title="Verleng met 7 dagen" aria-label="Verleng met 7 dagen">
                    {# clock #}
                    <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>
                    <span>+7d</span>
                  </button>
                </form>
                {% endif %}
                {% endif %}
                <form method="post" action="/uploads/{{ p.token }}/delete" style="display:inline"
                      onsubmit="return confirm('Pakket {{ p.title or p.token }} definitief verwijderen? Dit verwijdert ook de bestanden uit opslag.');">
                  <input type="hidden" name="_csrf" value="{{ csrf_token() }}">
                  <button class="oh-icon-btn danger" type="submit"
                          title="Pakket verwijderen" aria-label="Pakket verwijderen">
                    {# trash #}
                    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
                  </button>
                </form>
              </div>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      {% endif %}
    </div>
  </section>
</div>

<script>
function _copyToClipboard(link, btn){
  const orig = btn.innerHTML;
  const setTick = () => {
    btn.textContent = '✓';
    setTimeout(()=>{ btn.innerHTML = orig; }, 1600);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(link).then(setTick).catch(()=>{
      window.prompt('Kopieer de link:', link);
    });
  } else {
    window.prompt('Kopieer de link:', link);
  }
}

async function copyLink(btn){
  const needsUnlock = btn.getAttribute('data-needs-unlock') === '1';
  const token = btn.getAttribute('data-copy-token');

  // Snelle pad: pakket is van jezelf of al ontgrendeld -> link staat in dataset.
  if (!needsUnlock) {
    const link = btn.getAttribute('data-share-link') || '';
    if (link) { _copyToClipboard(link, btn); }
    return;
  }

  // Vergrendeld pakket: vraag de link op via de beveiligde endpoint.
  // De server controleert of dit pakket in deze sessie is ontgrendeld.
  const csrf = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';
  try {
    const resp = await fetch('/uploads/' + encodeURIComponent(token) + '/share-link', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRF-Token': csrf,
        'Accept': 'application/json'
      },
      body: JSON.stringify({})
    });
    if (resp.status === 403) {
      // Niet ontgrendeld -> stuur naar de unlock-pagina, met terugkeer naar deze lijst.
      const next = encodeURIComponent(window.location.pathname + window.location.search);
      window.location.href = '/p/unlock?token=' + encodeURIComponent(token) + '&next=' + next;
      return;
    }
    if (!resp.ok) {
      alert('Kon de link niet ophalen. Probeer het opnieuw.');
      return;
    }
    const data = await resp.json();
    if (data && data.ok && data.share_link) {
      // Cache de link op de knop zodat een tweede klik direct werkt.
      btn.setAttribute('data-needs-unlock', '0');
      btn.setAttribute('data-share-link', data.share_link);
      _copyToClipboard(data.share_link, btn);
    } else {
      alert('Kon de link niet ophalen.');
    }
  } catch (e) {
    alert('Netwerkfout bij ophalen van de link.');
  }
}

// Client-side sortering. Werkt via klikbare tabelkoppen (desktop) én via een
// dropdown + richtingsknop (mobiel, waar het tabelhoofd verborgen wordt).
(function(){
  const table = document.getElementById('ohTable');
  const tbody = document.getElementById('ohTableBody');
  if(!table || !tbody) return;
  const headers = table.querySelectorAll('th.sortable');
  if(!headers.length) return;

  const mobileField = document.getElementById('ohMobileSortField');
  const mobileDir   = document.getElementById('ohMobileSortDir');

  // Comparator per veldtype. Numerieke velden als Number, datums als ISO (string-vergelijk
  // werkt voor ISO-datums), strings als localeCompare.
  const NUMERIC = new Set(['size','downloads']);
  const DATELIKE = new Set(['created','expires']);

  function compareRows(a, b, field){
    const av = a.dataset[field] || '';
    const bv = b.dataset[field] || '';
    if(NUMERIC.has(field)){
      return (parseFloat(av) || 0) - (parseFloat(bv) || 0);
    }
    if(DATELIKE.has(field)){
      // ISO-strings sorteren alfabetisch correct op tijd.
      if(av < bv) return -1;
      if(av > bv) return 1;
      return 0;
    }
    return av.localeCompare(bv, 'nl', { sensitivity:'base' });
  }

  function findHeader(field){
    for(const th of headers){ if(th.dataset.sort === field) return th; }
    return null;
  }

  // Default-richting per kolom (zoals oorspronkelijk in de <th> gezet).
  const defaultDirs = {};
  headers.forEach(th => { defaultDirs[th.dataset.sort] = th.dataset.dir; });

  function setActive(field, dir){
    headers.forEach(th => {
      const arr = th.querySelector('.arr');
      if(th.dataset.sort === field){
        th.classList.add('active');
        th.dataset.dir = dir;
        if(arr) arr.textContent = dir === 'asc' ? '▲' : '▼';
      } else {
        th.classList.remove('active');
        if(arr) arr.textContent = '';
      }
    });
    // Sync mobile-controls.
    if(mobileField && mobileField.value !== field) mobileField.value = field;
    if(mobileDir){
      mobileDir.dataset.dir = dir;
      const arr = mobileDir.querySelector('.arr');
      if(arr) arr.textContent = dir === 'asc' ? '▲' : '▼';
    }
  }

  function applySort(field, dir){
    const rows = Array.from(tbody.querySelectorAll('tr'));
    rows.sort((a, b) => {
      const cmp = compareRows(a, b, field);
      return dir === 'asc' ? cmp : -cmp;
    });
    // Her-append in gesorteerde volgorde. Bestaande DOM-nodes blijven behouden,
    // dus geen flash/herrender van content, alleen reorder.
    const frag = document.createDocumentFragment();
    for(const r of rows) frag.appendChild(r);
    tbody.appendChild(frag);
    setActive(field, dir);
  }

  // Klik op een sorteerbare tabelkop.
  headers.forEach(th => {
    th.addEventListener('click', () => {
      const field = th.dataset.sort;
      let dir = th.dataset.dir;
      // Toggle richting als deze kolom al actief is.
      if(th.classList.contains('active')){
        dir = (dir === 'asc') ? 'desc' : 'asc';
      }
      applySort(field, dir);
    });
    th.addEventListener('keydown', (e) => {
      if(e.key === 'Enter' || e.key === ' '){ e.preventDefault(); th.click(); }
    });
    if(!th.hasAttribute('tabindex')) th.setAttribute('tabindex', '0');
    if(!th.hasAttribute('role')) th.setAttribute('role', 'button');
  });

  // Mobile: kolom kiezen in dropdown -> gebruik default-richting van die kolom.
  if(mobileField){
    mobileField.addEventListener('change', () => {
      const field = mobileField.value;
      const dir = defaultDirs[field] || 'asc';
      applySort(field, dir);
    });
  }
  // Mobile: richtingsknop togglet asc/desc voor de huidige kolom.
  if(mobileDir){
    mobileDir.addEventListener('click', () => {
      const field = mobileField ? mobileField.value : 'created';
      const dir = mobileDir.dataset.dir === 'asc' ? 'desc' : 'asc';
      applySort(field, dir);
    });
  }

  // Initiele sort: de kolom die al .active heeft (server-default = 'created' DESC).
  const initial = table.querySelector('th.sortable.active') || headers[0];
  if(initial){ applySort(initial.dataset.sort, initial.dataset.dir); }
})();
</script>
</body></html>
"""

def _package_summary(rows, now):
    """Bereken samenvatting van packagelijst."""
    pkg_count = len(rows)
    active = 0
    expired = 0
    total = 0
    for r in rows:
        total += int(r.get("size_bytes") or 0)
        exp = parse_dt_utc(r["expires_at"])
        if exp is None or exp <= now:
            expired += 1
        else:
            active += 1
    return {
        "pkg_count": pkg_count,
        "active": active,
        "expired": expired,
        "total_human": human(total),
    }

@app.route("/uploads")
def my_uploads():
    if not logged_in():
        return redirect(url_for("login"))
    me = current_user()
    if not me:
        session.clear()
        return redirect(url_for("login"))

    show_all = bool(me["is_admin"]) and (request.args.get("scope") == "all")
    tenant = me["tenant_id"]

    conn = db()
    try:
        # Vooraf: aggregaten in één scan per child-tabel ipv per-package subqueries.
        # Dit was de hoofdtraagheid bij 100+ pakketten (4 correlated subqueries
        # per package -> hier nog maar 2 GROUP BY's totaal).
        if show_all:
            rows = conn.execute("""
                SELECT p.token, p.title, p.expires_at, p.created_at, p.password_hash,
                       p.owner_user_id, u.email AS owner_email,
                       COALESCE(i.item_count, 0)     AS item_count,
                       COALESCE(i.size_bytes, 0)     AS size_bytes,
                       COALESCE(d.download_count, 0) AS download_count,
                       d.last_download_at            AS last_download_at
                FROM packages p
                LEFT JOIN users u ON u.id = p.owner_user_id
                LEFT JOIN (
                    SELECT token, tenant_id,
                           COUNT(*)         AS item_count,
                           SUM(size_bytes)  AS size_bytes
                    FROM items
                    WHERE tenant_id = ?
                    GROUP BY token, tenant_id
                ) i ON i.token = p.token AND i.tenant_id = p.tenant_id
                LEFT JOIN (
                    SELECT token, tenant_id,
                           COUNT(*)             AS download_count,
                           MAX(downloaded_at)   AS last_download_at
                    FROM download_events
                    WHERE tenant_id = ?
                    GROUP BY token, tenant_id
                ) d ON d.token = p.token AND d.tenant_id = p.tenant_id
                WHERE p.tenant_id = ?
                ORDER BY p.created_at DESC
            """, (tenant, tenant, tenant)).fetchall()
        else:
            rows = conn.execute("""
                SELECT p.token, p.title, p.expires_at, p.created_at, p.password_hash,
                       p.owner_user_id, ? AS owner_email,
                       COALESCE(i.item_count, 0)     AS item_count,
                       COALESCE(i.size_bytes, 0)     AS size_bytes,
                       COALESCE(d.download_count, 0) AS download_count,
                       d.last_download_at            AS last_download_at
                FROM packages p
                LEFT JOIN (
                    SELECT token, tenant_id,
                           COUNT(*)         AS item_count,
                           SUM(size_bytes)  AS size_bytes
                    FROM items
                    WHERE tenant_id = ?
                    GROUP BY token, tenant_id
                ) i ON i.token = p.token AND i.tenant_id = p.tenant_id
                LEFT JOIN (
                    SELECT token, tenant_id,
                           COUNT(*)             AS download_count,
                           MAX(downloaded_at)   AS last_download_at
                    FROM download_events
                    WHERE tenant_id = ?
                    GROUP BY token, tenant_id
                ) d ON d.token = p.token AND d.tenant_id = p.tenant_id
                WHERE p.tenant_id = ? AND p.owner_user_id = ?
                ORDER BY p.created_at DESC
            """, (me["email"], tenant, tenant, tenant, me["id"])).fetchall()
    finally:
        conn.close()

    now = datetime.now(timezone.utc)
    packages = []
    # Pakketten van een ándere collega vereisen — net als bij 'openen' — eerst
    # het MY_UPLOADS_PASSWORD voordat de share-link beschikbaar is. We geven
    # de link niet mee in de HTML; de JS haalt 'm pas op na unlock.
    feature_on = bool(MY_UPLOADS_PASSWORD)
    for r in rows:
        exp = parse_dt_utc(r["expires_at"]) or now
        is_expired = exp <= now
        # Onbeperkt geldig: vervaldatum staat ver in de toekomst (>= jaar 9000)
        is_never = exp.year >= 9000
        days_left = max(0, (exp - now).days) if not is_expired else 0
        # 'needs_unlock' = consistent met _needs_cross_user_unlock():
        #   feature aan, gebruiker ingelogd (per definitie, anders sta je hier niet),
        #   pakket is niet van jezelf, en pakket is in deze sessie nog niet ontgrendeld.
        is_own = (r["owner_user_id"] == me["id"])
        needs_unlock = (
            feature_on
            and not is_own
            and not _is_token_unlocked(r["token"])
        )
        packages.append({
            "token": r["token"],
            "title": r["title"],
            "expires_at": r["expires_at"],
            "created_at": r["created_at"],
            "item_count": int(r["item_count"] or 0),
            "size_bytes": int(r["size_bytes"] or 0),
            "size_human": human(int(r["size_bytes"] or 0)),
            "has_password": bool(r["password_hash"]),
            "is_expired": is_expired,
            "is_never": is_never,
            "days_left": days_left,
            "owner_email": r["owner_email"],
            "download_count": int(r["download_count"] or 0),
            "last_download_at": format_nl_datetime(r["last_download_at"]),
            "needs_unlock": needs_unlock,
            # Share-link alléén meegeven als het pakket niet vergrendeld is.
            # Anders kun je 'm gewoon uit de HTML-bron lezen en is de
            # bescherming waardeloos.
            "share_link": "" if needs_unlock else url_for("package_page", token=r["token"], _external=True),
        })

    summary = _package_summary([dict(r) for r in rows], now)
    msg, err = _pop_flash()

    return render_template_string(
        MY_UPLOADS_HTML,
        packages=packages,
        summary=summary,
        show_all=show_all,
        is_admin=bool(me["is_admin"]),
        user=me["email"],
        flash_msg=msg, flash_err=err,
        base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON
    )

@app.route("/uploads/<token>/share-link", methods=["POST"])
def my_uploads_share_link(token):
    """Geef de share-link van een pakket terug — maar alleen als de huidige
    sessie het pakket mag zien.

    Gebruikt door de 'Link kopiëren'-knop in 'Mijn uploads'. Hiermee staat de
    URL van een vergrendeld pakket NIET in de HTML-bron: hij wordt pas na
    server-side check uitgeleverd. Dezelfde regels als bij 'openen':
      - Eigen pakket of feature uit: direct toegestaan.
      - Pakket van een ander, niet ontgrendeld in deze sessie: 403.
      - Wel ontgrendeld in deze sessie: toegestaan.
    """
    if not logged_in():
        return jsonify(ok=False, error="unauthorized"), 401
    me = current_user()
    if not me:
        return jsonify(ok=False, error="unauthorized"), 401

    token = (token or "").strip().lower()
    if not is_valid_token(token):
        return jsonify(ok=False, error="bad_token"), 400

    conn = db()
    try:
        row = conn.execute(
            "SELECT token, owner_user_id FROM packages WHERE token = ? AND tenant_id = ?",
            (token, me["tenant_id"])
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return jsonify(ok=False, error="not_found"), 404

    # Cross-user check, identiek aan _needs_cross_user_unlock maar zonder
    # de 'not logged_in()' shortcut: we wéten dat we ingelogd zijn.
    is_own = (row["owner_user_id"] == me["id"])
    if MY_UPLOADS_PASSWORD and not is_own and not _is_token_unlocked(token):
        # Geef 403 terug; de JS stuurt de gebruiker dan naar /p/unlock.
        return jsonify(ok=False, error="locked"), 403

    share_link = url_for("package_page", token=token, _external=True)
    return jsonify(ok=True, share_link=share_link)


@app.route("/uploads/<token>/delete", methods=["POST"])
def my_uploads_delete(token):
    if not logged_in(): abort(401)
    me = current_user()
    if not me: abort(401)
    token = (token or "").strip()
    if not is_valid_token(token):
        _flash(error="Ongeldig token.")
        return redirect(url_for("my_uploads"))

    conn = db()
    try:
        pkg = conn.execute(
            "SELECT token, owner_user_id, tenant_id FROM packages WHERE token = ? AND tenant_id = ?",
            (token, me["tenant_id"])
        ).fetchone()
        if not pkg:
            _flash(error="Pakket niet gevonden.")
            return redirect(url_for("my_uploads"))
        if pkg["owner_user_id"] != me["id"] and not me["is_admin"]:
            _flash(error="Geen rechten om dit pakket te verwijderen.")
            return redirect(url_for("my_uploads"))

        # Verwijder S3-objects
        items = conn.execute(
            "SELECT s3_key FROM items WHERE token = ? AND tenant_id = ?",
            (token, me["tenant_id"])
        ).fetchall()
        for it in items:
            try:
                s3.delete_object(Bucket=S3_BUCKET, Key=it["s3_key"])
            except Exception:
                log.exception("Kon S3 object niet verwijderen: %s", it["s3_key"])

        conn.execute("DELETE FROM items WHERE token = ? AND tenant_id = ?", (token, me["tenant_id"]))
        conn.execute("DELETE FROM packages WHERE token = ? AND tenant_id = ?", (token, me["tenant_id"]))
        conn.commit()
        _flash(msg=f"Pakket verwijderd ({len(items)} bestand(en) uit opslag).")
    finally:
        conn.close()
    # Behoud scope-parameter
    scope = request.args.get("scope") or request.referrer or ""
    if "scope=all" in scope:
        return redirect(url_for("my_uploads", scope="all"))
    return redirect(url_for("my_uploads"))

@app.route("/uploads/<token>/extend", methods=["POST"])
def my_uploads_extend(token):
    if not logged_in(): abort(401)
    me = current_user()
    if not me: abort(401)
    token = (token or "").strip()
    if not is_valid_token(token):
        _flash(error="Ongeldig token.")
        return redirect(url_for("my_uploads"))

    conn = db()
    try:
        pkg = conn.execute(
            "SELECT token, owner_user_id, expires_at FROM packages WHERE token = ? AND tenant_id = ?",
            (token, me["tenant_id"])
        ).fetchone()
        if not pkg:
            _flash(error="Pakket niet gevonden.")
            return redirect(url_for("my_uploads"))
        if pkg["owner_user_id"] != me["id"] and not me["is_admin"]:
            _flash(error="Geen rechten om dit pakket te verlengen.")
            return redirect(url_for("my_uploads"))

        current_exp = parse_dt_utc(pkg["expires_at"]) or datetime.now(timezone.utc)
        # Onbeperkt geldige pakketten hoeven (en kunnen) niet verlengd worden:
        # +7 dagen op een datum in jaar 9999 zou overflowen.
        if current_exp.year >= 9000:
            _flash(msg="Dit pakket is al onbeperkt geldig.")
            return redirect(url_for("my_uploads"))
        # Verleng met 7 dagen vanaf de huidige vervaldatum (of nu, als al verlopen)
        base = max(current_exp, datetime.now(timezone.utc))
        new_exp = base + timedelta(days=7)
        conn.execute(
            "UPDATE packages SET expires_at = ? WHERE token = ? AND tenant_id = ?",
            (new_exp.isoformat(), token, me["tenant_id"])
        )
        conn.commit()
        _flash(msg=f"Pakket verlengd tot {new_exp.strftime('%d-%m-%Y %H:%M')}.")
    finally:
        conn.close()
    return redirect(url_for("my_uploads"))


@app.errorhandler(400)
def handle_400(err):
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="bad_request"), 400
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>400</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>400</h1><p>Het verzoek kon niet worden verwerkt.</p><p><a class="btn-pro primary" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON), 400

@app.errorhandler(401)
def handle_401(err):
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="unauthorized"), 401
    return redirect(url_for("login"))

@app.errorhandler(404)
def handle_404(err):
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>404</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>404</h1><p>Deze pagina of download bestaat niet (meer).</p><p><a class="btn-pro primary" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON), 404

@app.errorhandler(413)
def handle_413(err):
    return jsonify(ok=False, error="payload_too_large", max_bytes=app.config.get("MAX_CONTENT_LENGTH")), 413

@app.errorhandler(500)
def handle_500(err):
    log.exception("Unhandled server error", exc_info=err)
    if request.path.startswith(("/package-init", "/put-", "/mpu-", "/billing/", "/internal/", "/webhook/")):
        return jsonify(ok=False, error="server_error", request_id=getattr(g, "request_id", None)), 500
    return render_template_string("""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">{{ head_icon|safe }}<title>500</title><style>{{ base_css|safe }}</style></head><body>{{ bg|safe }}<div class="shell"><div class="card"><h1>500</h1><p>Er ging iets mis op de server.</p><p>Referentie: <code>{{ request_id }}</code></p><p><a class="btn-pro primary" href="/">Terug naar home</a></p></div></div></body></html>""", base_css=BASE_CSS, bg=BG_DIV, head_icon=HTML_HEAD_ICON, request_id=getattr(g, "request_id", "-")), 500

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
    """Alias naar _client_ip — single source of truth.

    Eerder werd hier X-Forwarded-For zelf opnieuw geparset; dat dupliceerde
    werk dat ProxyFix al doet en kon bij multi-hop proxies een andere IP
    teruggeven dan _client_ip(). Nu consistent.
    """
    return _client_ip()


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
