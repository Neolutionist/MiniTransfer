#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ========= MiniTransfer – Olde Hanter (click radio => open picker; animated bg) =========
# - Login
# - Upload (files/folders) naar Backblaze B2 (S3)
# - Single PUT < 5 MB (incl. 0 bytes)  | Multipart ≥ 5 MB
# - Downloadpagina met voortgang + "alles zippen" (compat met meerdere zipstream-ng API’s)
# - Titel (onderwerp) per pakket
# - CTA sticky onderaan de card
# - iOS/iPhone: map-upload uitgeschakeld en verborgen
# - Klik op "Bestand(en)" of "Map" opent direct het selectievenster (upload start NIET automatisch)
# ========================================================================

import os, re, uuid, smtplib, sqlite3, logging
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import (
    Flask, request, redirect, url_for, abort, render_template_string,
    session, jsonify, Response, stream_with_context
)
from werkzeug.utils import secure_filename
    # wachtwoord is optioneel; hash voor pakketbeveiliging
from werkzeug.security import generate_password_hash, check_password_hash

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError
from zipstream import ZipStream  # zipstream-ng

# ---------------- Config ----------------
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "files_multi.db"

AUTH_EMAIL = "info@oldehanter.nl"
AUTH_PASSWORD = "Hulsmaat"

# Backblaze (S3 compat)
S3_BUCKET       = os.environ["S3_BUCKET"]
S3_REGION       = os.environ.get("S3_REGION", "eu-central-003")
S3_ENDPOINT_URL = os.environ["S3_ENDPOINT_URL"]

# SMTP (optioneel)
SMTP_HOST = os.environ.get("SMTP_HOST")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")
SMTP_FROM = os.environ.get("SMTP_FROM") or SMTP_USER
MAIL_TO   = os.environ.get("MAIL_TO", "Patrick@oldehanter.nl")

def smtp_configured():
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASS)

# S3 client (SigV4 + path-style → Backblaze-proof)
s3 = boto3.client(
    "s3",
    region_name=S3_REGION,
    endpoint_url=S3_ENDPOINT_URL,
    config=BotoConfig(s3={"addressing_style": "path"}, signature_version="s3v4"),
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me")

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

# --------------- DB --------------------
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def _column_exists(cursor, table, column):
    rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
    names = {r[1] for r in rows}
    return column in names

def init_db():
    c = db()
    c.execute("""
      CREATE TABLE IF NOT EXISTS packages (
        token TEXT PRIMARY KEY,
        expires_at TEXT NOT NULL,
        password_hash TEXT,
        created_at TEXT NOT NULL
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
    if not _column_exists(c, "packages", "title"):
        try: c.execute("ALTER TABLE packages ADD COLUMN title TEXT")
        except Exception: pass
    c.commit(); c.close()
init_db()

# -------------- CSS --------------
BASE_CSS = """
*,*:before,*:after{box-sizing:border-box}
:root{
  --bg1:#60a5fa; --bg2:#a78bfa; --bg3:#34d399;
  --panel:rgba(255,255,255,.82); --panel-b:rgba(255,255,255,.45);
  --brand:#003366; --brand-2:#0f4c98;
  --text:#0f172a; --muted:#475569; --line:#d1d5db; --ring:#2563eb;
  --surface:#ffffff; --surface-2:#f1f5f9;
}
html,body{height:100%}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);margin:0;position:relative;overflow-x:hidden}

/* Dynamische achtergrond: multi-layer gradient blobs + subtiele beweging */
.bg{position:fixed;inset:0;z-index:-2;overflow:hidden;background:linear-gradient(180deg,#eef3fb 0%,#e9eef6 100%)}
.bg .blob{
  position:absolute;filter:blur(40px);opacity:.55;will-change:transform;border-radius:50%;
}
.blob.b1{width:60vmax;height:60vmax;left:-10vmax;top:-6vmax;background:radial-gradient(closest-side,var(--bg1),transparent 66%);animation:float1 18s ease-in-out infinite}
.blob.b2{width:55vmax;height:55vmax;right:-12vmax;top:-8vmax;background:radial-gradient(closest-side,var(--bg2),transparent 66%);animation:float2 22s ease-in-out infinite}
.blob.b3{width:60vmax;height:60vmax;left:10vmax;bottom:-18vmax;background:radial-gradient(closest-side,var(--bg3),transparent 66%);animation:float3 26s ease-in-out infinite}
@keyframes float1{0%{transform:translate3d(0,0,0)}50%{transform:translate3d(4%,3%,0)}100%{transform:translate3d(0,0,0)}}
@keyframes float2{0%{transform:translate3d(0,0,0)}50%{transform:translate3d(-3%,5%,0)}100%{transform:translate3d(0,0,0)}}
@keyframes float3{0%{transform:translate3d(0,0,0)}50%{transform:translate3d(5%,-4%,0)}100%{transform:translate3d(0,0,0)}}

.wrap{max-width:980px;margin:6vh auto;padding:0 1rem}
.card{padding:1.5rem;background:var(--panel);border:1px solid var(--panel-b);
      border-radius:18px;box-shadow:0 18px 40px rgba(0,0,0,.12);backdrop-filter: blur(10px)}
h1{line-height:1.15}
.footer{color:#334155;margin-top:1.2rem;text-align:center}
.small{font-size:.9rem;color:var(--muted)}
label{display:block;margin:.65rem 0 .35rem;font-weight:600;color:var(--text)}
.input, input[type=text], input[type=password], input[type=email], input[type=number],
select, textarea{
  width:100%; display:block; appearance:none;
  padding: .85rem 1rem; border-radius:12px; border:1px solid var(--line);
  background:#f0f6ff; color:var(--text);
  outline: none; transition: box-shadow .15s, border-color .15s, background .15s;
}
input:focus, .input:focus, select:focus, textarea:focus{
  border-color: var(--ring); box-shadow: 0 0 0 4px rgba(37,99,235,.15);
}
input[type=file]{padding:.55rem 1rem; background:#f0f6ff; cursor:pointer}
input[type=file]::file-selector-button{
  margin-right:.75rem; border:1px solid var(--line);
  background:var(--surface-2); color:var(--text);
  padding:.55rem .9rem; border-radius:10px; cursor:pointer;
}

/* Buttons & progress */
.btn{
  padding:.85rem 1.05rem;border:0;border-radius:12px;
  background:var(--brand);color:#fff;font-weight:700;cursor:pointer;
  box-shadow:0 4px 14px rgba(0,51,102,.25); transition:filter .15s, transform .02s;
  font-size:.95rem; line-height:1;
}
.btn.small{padding:.55rem .8rem;font-size:.9rem}
.btn:hover{filter:brightness(1.05)}
.btn:active{transform:translateY(1px)}
.btn.secondary{background:var(--brand-2)}
.progress{height:10px;background:#e5ecf6;border-radius:999px;overflow:hidden;margin-top:.75rem}
.progress > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#0f4c98,#1e90ff);transition:width .1s}

/* Downloadpagina mobile-friendly */
.table{width:100%;border-collapse:collapse;margin-top:.6rem}
.table th,.table td{padding:.55rem .7rem;border-bottom:1px solid #e5e7eb;text-align:left}
@media (max-width: 680px){
  .table thead{display:none}
  .table, .table tbody, .table tr, .table td{display:block;width:100%}
  .table tr{margin-bottom:.6rem;background:rgba(255,255,255,.55);border:1px solid #e5e7eb;border-radius:10px;padding:.4rem .6rem}
  .table td{border:0;padding:.25rem 0}
  .table td[data-label]:before{content:attr(data-label) ": ";font-weight:600;color:#334155}
}

/* CTA vast onderaan de card, niet meescrollen */
.cta-fixed{
  position:sticky; bottom:0; display:flex; justify-content:center;
  padding:1rem; margin-top:1rem;
  background:linear-gradient(180deg,transparent,rgba(0,0,0,.03));
}
"""

# -------------- Templates --------------
LOGIN_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen – Olde Hanter</title><style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>
<div class="wrap"><div class="card">
  <h1>Inloggen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}
  <form method="post" autocomplete="on">
    <label for="email">E-mail</label>
    <input id="email" class="input" name="email" type="email" value="info@oldehanter.nl" autocomplete="username" required>
    <label for="pw">Wachtwoord</label>
    <input id="pw" class="input" name="password" type="password" placeholder="Wachtwoord" autocomplete="current-password" required>
    <button class="btn" type="submit" style="margin-top:1rem">Inloggen</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

INDEX_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Olde Hanter – Upload</title>
<style>
{{ base_css }}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
h1{margin:.25rem 0 1rem;color:var(--brand);font-size:2.1rem}
.logout a{color:var(--brand);text-decoration:none;font-weight:700}
.toggle{display:flex;gap:.75rem;align-items:center;margin:.4rem 0 1rem}
</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>

<div class="wrap">
  <div class="topbar">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div class="logout">Ingelogd als {{ user }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  <form id="f" class="card" enctype="multipart/form-data" autocomplete="off">
    <label>Uploadtype</label>
    <div class="toggle">
      <label id="lblFiles"><input id="modeFiles" type="radio" name="upmode" value="files" checked> Bestand(en)</label>
      <label id="lblFolder"><input id="modeFolder" type="radio" name="upmode" value="folder"> Map</label>
    </div>

    <div id="fileRow">
      <label for="fileInput">Kies bestand(en)</label>
      <input id="fileInput" class="input" type="file" multiple>
    </div>

    <div id="folderRow" style="display:none">
      <label for="folderInput">Kies een map</label>
      <input id="folderInput" class="input" type="file" multiple webkitdirectory directory>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-top:.6rem">
      <div>
        <label for="title">Onderwerp (optioneel)</label>
        <input id="title" class="input" type="text" placeholder="Bijv. Tekeningen project X" maxlength="120">
      </div>
      <div>
        <label for="exp">Verloopt over (dagen)</label>
        <input id="exp" class="input" type="number" min="1" value="24">
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr;gap:1rem;margin-top:.6rem">
      <div>
        <label for="pw">Wachtwoord (optioneel)</label>
        <input id="pw" class="input" type="password" placeholder="Laat leeg voor geen wachtwoord" autocomplete="new-password" autocapitalize="off" spellcheck="false">
      </div>
    </div>

    <button class="btn" type="submit" style="margin-top:1rem">Uploaden</button>
    <div class="progress" id="upbar" style="display:none"><i></i></div>
    <div class="small" id="uptext" style="display:none">0%</div>
  </form>

  <div id="result"></div>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  // --- iOS detectie: Map-upload uitschakelen en verbergen ---
  function isIOS(){
    const ua = navigator.userAgent || navigator.vendor || window.opera;
    const iOSUA = /iPad|iPhone|iPod/.test(ua);
    const iPadOS = (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    return iOSUA || iPadOS;
  }
  const form = document.getElementById('f');
  const modeFiles = document.getElementById('modeFiles');
  const modeFolder = document.getElementById('modeFolder');
  const lblFiles = document.getElementById('lblFiles');
  const lblFolder = document.getElementById('lblFolder');

  if(isIOS()){
    modeFolder.disabled = true;
    lblFolder.style.display = 'none';
    modeFiles.checked = true;
  }

  // Toggle bestand/map
  const modeRadios = document.querySelectorAll('input[name="upmode"]');
  const fileRow = document.getElementById('fileRow');
  const folderRow = document.getElementById('folderRow');
  const fileInput   = document.getElementById('fileInput');
  const folderInput = document.getElementById('folderInput');

  function applyMode(openPicker){
    const mode = document.querySelector('input[name="upmode"]:checked').value;
    fileRow.style.display  = (mode==='files')  ? '' : 'none';
    folderRow.style.display = (mode==='folder') ? '' : 'none';

    // Als dit uit een klik komt → open direct de juiste picker (upload start niet automatisch)
    if(openPicker === true){
      try{
        (mode === 'files' ? fileInput : folderInput).click();
      }catch(e){}
    }
  }
  modeRadios.forEach(r => r.addEventListener('change', ()=>applyMode(true)));

  // Ook klikken op het label opent direct de picker (extra gebruiksgemak)
  lblFiles.addEventListener('click', (e)=>{
    // als al actief, open toch de picker
    setTimeout(()=>{ try{ fileInput.click(); }catch(e){} }, 0);
  });
  lblFolder.addEventListener('click', (e)=>{
    if(isIOS()) return; // map niet ondersteund op iOS
    setTimeout(()=>{ try{ folderInput.click(); }catch(e){} }, 0);
  });

  applyMode(false);

  // ---- Helpers ----
  const resBox = document.getElementById('result');
  const upbar = document.getElementById('upbar');
  const upbarFill = upbar.querySelector('i');
  const uptext = document.getElementById('uptext');

  function gatherFiles(){
    const mode = document.querySelector('input[name="upmode"]:checked').value;
    return (mode==='files') ? fileInput.files : folderInput.files;
  }
  function relPath(f){
    const mode = document.querySelector('input[name="upmode"]:checked').value;
    return (mode==='files') ? f.name : (f.webkitRelativePath || f.name);
  }

  async function packageInit(expiryDays, password, title){
    const r = await fetch("{{ url_for('package_init') }}", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ expiry_days: expiryDays, password: password || "", title: title || "" })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Kan pakket niet starten");
    return j.token;
  }

  // Single PUT (<5MB)
  async function singleInit(token, filename, type){
    const r = await fetch("{{ url_for('put_init') }}", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ token, filename, contentType: type || "application/octet-stream" })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Init (PUT) mislukt");
    return j; // {key, url}
  }
  async function singleComplete(token, key, name, path){
    const r = await fetch("{{ url_for('put_complete') }}", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ token, key, name, path })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Afronden (PUT) mislukt");
    return j;
  }

  // Multipart (≥5MB)
  async function mpuInit(token, filename, type){
    const r = await fetch("{{ url_for('mpu_init') }}", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ token, filename, contentType: type || "application/octet-stream" })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Init (MPU) mislukt");
    return j; // {key, uploadId}
  }
  async function signPart(key, uploadId, partNumber){
    const r = await fetch("{{ url_for('mpu_sign') }}", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ key, uploadId, partNumber })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Sign part mislukt");
    return j.url;
  }
  async function mpuComplete(token, key, name, path, parts, uploadId){
    const r = await fetch("{{ url_for('mpu_complete') }}", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ token, key, name, path, parts, uploadId })
    });
    const j = await r.json();
    if(!r.ok || !j.ok) throw new Error(j.error || "Afronden (MPU) mislukt");
    return j;
  }

  function putWithProgress(url, blob, updateCb, label){
    return new Promise((resolve,reject)=>{
      const xhr = new XMLHttpRequest();
      xhr.open("PUT", url, true);
      xhr.timeout = 300000;
      xhr.setRequestHeader("Content-Type", blob.type || "application/octet-stream");
      xhr.upload.onprogress = (ev)=> updateCb(ev.loaded);
      xhr.onload = ()=>{
        if(xhr.status>=200 && xhr.status<300){
          const etag = xhr.getResponseHeader("ETag");
          resolve(etag ? etag.replaceAll('\"','') : null);
        } else {
          reject(new Error(`HTTP ${xhr.status} ${xhr.statusText||''} bij ${label||'upload'}: ${xhr.responseText||''}`));
        }
      };
      xhr.onerror   = ()=> reject(new Error(`Netwerkfout bij ${label||'upload'} (CORS/endpoint?)`));
      xhr.ontimeout = ()=> reject(new Error(`Timeout bij ${label||'upload'}`));
      xhr.send(blob);
    });
  }

  async function uploadSingle(token, file, relpath, totalTracker){
    const init = await singleInit(token, file.name, file.type);
    await putWithProgress(init.url, file, (loaded)=>{
      const total = totalTracker.currentBase + loaded;
      const denom = totalTracker.totalBytes || 1;
      const p = Math.round(total / denom * 100);
      upbarFill.style.width = Math.min(p,100) + "%";
      uptext.textContent = (p<100? p+"%" : "100% – verwerken…");
    }, 'PUT object');
    await singleComplete(token, init.key, file.name, relpath);
    totalTracker.currentBase += file.size;
  }

  async function uploadMultipart(token, file, relpath, totalTracker){
    const CHUNK = 5 * 1024 * 1024;
    const init = await mpuInit(token, file.name, file.type);
    const key = init.key, uploadId = init.uploadId;

    const parts = Math.ceil(Math.max(1, file.size) / CHUNK);
    const perPart = new Array(parts).fill(0);

    function updateBar(){
      const uploadedThis = perPart.reduce((a,b)=>a+b,0);
      const total = totalTracker.currentBase + uploadedThis;
      const denom = totalTracker.totalBytes || 1;
      const p = Math.round(total / denom * 100);
      upbarFill.style.width = Math.min(p,100) + "%";
      uptext.textContent = (p<100? p+"%" : "100% – verwerken…");
    }

    async function uploadPart(partNumber){
      const idx   = partNumber - 1;
      const start = idx * CHUNK;
      const end   = Math.min(start + CHUNK, file.size);
      const blob  = file.slice(start, end);

      const MAX_TRIES = 6;
      for(let attempt=1; attempt<=MAX_TRIES; attempt++){
        try{
          const url  = await signPart(key, uploadId, partNumber);
          const etag = await putWithProgress(url, blob, (loaded)=>{ perPart[idx] = loaded; updateBar(); }, `part ${partNumber}`);
          perPart[idx] = blob.size; updateBar();
          return { PartNumber: partNumber, ETag: etag };
        }catch(err){
          if(attempt===MAX_TRIES) throw err;
          const backoff = Math.round(500 * Math.pow(2, attempt-1) * (0.85 + Math.random()*0.3));
          await new Promise(r=>setTimeout(r, backoff));
        }
      }
    }

    const results = [];
    for(let pn=1; pn<=parts; pn++) results.push(await uploadPart(pn));

    await mpuComplete(token, key, file.name, relpath, results, uploadId);
    totalTracker.currentBase += file.size;
  }

  // Handmatige submit (knop) start de upload
  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const mode = document.querySelector('input[name="upmode"]:checked').value;
    const files = Array.from((mode==='files' ? fileInput.files : folderInput.files) || []);
    if(!files.length){
      alert("Kies bestand(en)"+(isIOS()?"":" of map"));
      // open de juiste picker als er nog niets geselecteerd is
      try { (mode==='files'? fileInput : folderInput).click(); } catch(e){}
      return;
    }

    const expiryDays = document.getElementById('exp').value || '24';
    const password   = document.getElementById('pw').value || '';
    const title      = document.getElementById('title').value || '';

    const totalBytes = files.reduce((a,f)=>a+f.size,0) || 1;
    const tracker = { totalBytes, currentBase: 0 };

    upbar.style.display='block'; uptext.style.display='block';
    upbarFill.style.width='0%'; uptext.textContent='0%';

    try{
      const token = await packageInit(expiryDays, password, title);

      for(const f of files){
        const rel = relPath(f);
        if(f.size < 5 * 1024 * 1024){
          await uploadSingle(token, f, rel, tracker);
        }else{
          await uploadMultipart(token, f, rel, tracker);
        }
      }

      upbarFill.style.width='100%'; uptext.textContent='Klaar';
      const link = "{{ url_for('package_page', token='__T__', _external=True) }}".replace("__T__", token);

      document.getElementById('result').innerHTML = `
        <div class="card" style="margin-top:1rem">
          <strong>Deelbare link</strong>
          <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
            <input class="input" style="flex:1" value="${link}" readonly>
            <button class="btn" type="button"
              onclick="(navigator.clipboard?.writeText('${link}')||Promise.reject()).then(()=>alert('Link gekopieerd'))">
              Kopieer
            </button>
          </div>
        </div>`;
    }catch(err){
      alert(err.message || 'Onbekende fout');
    }
  });
</script>
</body></html>
"""

PACKAGE_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Download – Olde Hanter</title>
<style>
{{ base_css }}
h1{margin:.2rem 0 1rem;color:var(--brand)}
.meta{margin:.4rem 0 1rem;color:#374151}
.btn{padding:.85rem 1.15rem;border-radius:12px;background:var(--brand);color:#fff;text-decoration:none;font-weight:700}
.btn.secondary{background:#0f4c98}
.btn.mini{padding:.5rem .75rem;font-size:.9rem;border-radius:10px}
</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>

<div class="wrap">
  <div class="card">
    <h1>Download</h1>
    <div class="meta">
      <div><strong>Pakket:</strong> {{ title or token }}</div>
      <div><strong>Verloopt:</strong> {{ expires_human }}</div>
      <div><strong>Totaal:</strong> {{ total_human }}</div>
      <div><strong>Bestanden:</strong> {{ items|length }}</div>
    </div>

    {% if items|length == 1 %}
      <button class="btn" id="dlBtn">Download</button>
    {% else %}
      <button class="btn" id="zipAll">Alles downloaden (zip)</button>
    {% endif %}
    <div class="progress" id="bar" style="display:none"><i></i></div>
    <div class="small" id="txt" style="display:none">Starten…</div>

    {% if items|length > 1 %}
    <table class="table">
      <thead><tr><th>Bestand</th><th>Pad</th><th>Grootte</th><th style="width:1%"></th></tr></thead>
      <tbody>
      {% for it in items %}
        <tr>
          <td data-label="Bestand">{{ it["name"] }}</td>
          <td class="small" data-label="Pad">{{ it["path"] }}</td>
          <td data-label="Grootte">{{ it["size_h"] }}</td>
          <td data-label=""><a class="btn mini" href="{{ url_for('stream_file', token=token, item_id=it['id']) }}">Download</a></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
    {% endif %}

    <div class="linkbox" style="margin-top:1rem">
      <div><strong>Deelbare link</strong></div>
      <div style="display:flex;gap:.5rem;align-items:center;">
        <input class="input" type="text" id="shareLink" value="{{ share_link }}" readonly>
        <button class="btn" type="button"
          onclick="(navigator.clipboard?.writeText('{{ share_link }}')||Promise.reject()).then(()=>alert('Link gekopieerd'))">
          Kopieer
        </button>
      </div>
    </div>

    <div class="cta-fixed">
      <a class="btn secondary" href="{{ url_for('contact') }}">Eigen transfer-oplossing aanvragen</a>
    </div>
  </div>

  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div>

<script>
  const bar = document.getElementById('bar');
  const fill = bar?.querySelector('i');
  const txt = document.getElementById('txt');

  async function streamToBlob(url, fallbackName){
    bar.style.display='block'; txt.style.display='block';
    fill.style.width='0%'; txt.textContent='Starten…';
    const res = await fetch(url);
    if(!res.ok){
      const xerr = res.headers.get('X-Error') || '';
      let body = '';
      try { body = await res.text(); } catch(e){}
      alert(`Fout ${res.status}${xerr ? ' – '+xerr : ''}${body ? '\\n\\n'+body : ''}`);
      return;
    }
    const total = parseInt(res.headers.get('Content-Length')||'0',10);
    const name  = res.headers.get('X-Filename') || fallbackName || 'download.zip';

    const reader = res.body.getReader(); const chunks=[]; let received=0;
    while(true){
      const {done,value} = await reader.read();
      if(done) break;
      chunks.push(value); received += value.length;
      if(total){ const p=Math.round(received/total*100); fill.style.width=p+'%'; txt.textContent=p+'%'; }
      else { txt.textContent = (received/1024/1024).toFixed(1)+' MB…'; }
    }
    fill.style.width='100%'; txt.textContent='Klaar';
    const blob = new Blob(chunks);
    const u = URL.createObjectURL(blob); const a = document.createElement('a');
    a.href=u; a.download=name; document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(u);
  }

  {% if items|length == 1 %}
    document.getElementById('dlBtn')?.addEventListener('click', ()=>{
      streamToBlob("{{ url_for('stream_file', token=token, item_id=items[0]['id']) }}", "{{ items[0]['name'] }}");
    });
  {% else %}
    document.getElementById('zipAll')?.addEventListener('click', ()=>{
      streamToBlob("{{ url_for('stream_zip', token=token) }}", "download.zip");
    });
  {% endif %}
</script>
</body></html>
"""

CONTACT_HTML = """
<!doctype html><html lang="nl"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eigen transfer-oplossing – Olde Hanter</title><style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>
<div class="wrap"><div class="card">
  <h1>Eigen transfer-oplossing aanvragen</h1>
  {% if error %}<div style="background:#fee2e2;color:#991b1b;padding:.6rem .8rem;border-radius:10px;margin-bottom:1rem">{{ error }}</div>{% endif %}
  <form method="post" action="{{ url_for('contact') }}" novalidate>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem">
      <div>
        <label for="login_email">Gewenste inlog-e-mail</label>
        <input id="login_email" class="input" name="login_email" type="email" placeholder="naam@bedrijf.nl" value="{{ form.login_email or '' }}" required>
      </div>
      <div>
        <label for="storage_tb">Gewenste opslaggrootte</label>
        <select id="storage_tb" class="input" name="storage_tb" required>
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
        <input id="company" class="input" name="company" type="text" placeholder="Bedrijfsnaam BV" value="{{ form.company or '' }}" minlength="2" maxlength="100" required>
      </div>
      <div>
        <label for="phone">Telefoonnummer</label>
        <input id="phone" class="input" name="phone" type="tel" placeholder="+31 6 12345678" value="{{ form.phone or '' }}" pattern="^[0-9+()\\s-]{8,20}$" required>
      </div>
    </div>
    <button class="btn" type="submit" style="margin-top:1rem">Verstuur aanvraag</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

CONTACT_DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag verstuurd</title><style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>
<div class="wrap"><div class="card"><h1>Dank je wel!</h1><p>Je aanvraag is verstuurd. We nemen zo snel mogelijk contact met je op.</p><p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div>
</body></html>
"""

CONTACT_MAIL_FALLBACK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title><style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true">
  <i class="blob b1"></i><i class="blob b2"></i><i class="blob b3"></i>
</div>
<div class="wrap"><div class="card">
  <h1>Aanvraag gereed</h1>
  <p>SMTP staat niet ingesteld of gaf een fout. Klik op de knop hieronder om de e-mail te openen in je mailprogramma.</p>
  <a class="btn" href="{{ mailto_link }}">Open e-mail</a>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

# -------------- Helpers / routes --------------
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
        s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)

@app.route("/")
def index():
    if not logged_in(): return redirect(url_for("login"))
    return render_template_string(INDEX_HTML, user=session.get("user"), base_css=BASE_CSS)

@app.route("/login", methods=["GET","POST"])
def login():
    if request.method == "POST":
        if (request.form.get("email") or "").lower()==AUTH_EMAIL and request.form.get("password")==AUTH_PASSWORD:
            session["authed"] = True; session["user"] = AUTH_EMAIL
            return redirect(url_for("index"))
        return render_template_string(LOGIN_HTML, error="Onjuiste inloggegevens.", base_css=BASE_CSS)
    return render_template_string(LOGIN_HTML, error=None, base_css=BASE_CSS)

@app.route("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# API: packages + uploads
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
    c = db()
    c.execute("INSERT INTO packages(token,expires_at,password_hash,created_at,title) VALUES(?,?,?,?,?)",
              (token, expires_at, pw_hash, datetime.now(timezone.utc).isoformat(), title))
    c.commit(); c.close()
    return jsonify(ok=True, token=token)

# Single PUT
@app.route("/put-init", methods=["POST"])
def put_init():
    if not logged_in(): abort(401)
    d = request.get_json(force=True, silent=True) or {}
    token = d.get("token"); filename = secure_filename(d.get("filename") or "")
    content_type = d.get("contentType") or "application/octet-stream"
    if not token or not filename:
        return jsonify(ok=False, error="Onvolledige init (PUT)"), 400
    key = f"uploads/{token}/{uuid.uuid4().hex[:8]}__{filename}"
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
        c = db()
        c.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes) VALUES(?,?,?,?,?)""",
                  (token, key, name, path, size))
        c.commit(); c.close()
        return jsonify(ok=True)
    except (ClientError, BotoCoreError):
        log.exception("put_complete failed")
        return jsonify(ok=False, error="server_error"), 500

# Multipart
@app.route("/mpu-init", methods=["POST"])
def mpu_init():
    if not logged_in(): abort(401)
    data = request.get_json(force=True, silent=True) or {}
    token = data.get("token")
    filename = secure_filename(data.get("filename") or "")
    content_type = data.get("contentType") or "application/octet-stream"
    if not token or not filename:
        return jsonify(ok=False, error="Onvolledige init (MPU)"), 400
    key = f"uploads/{token}/{uuid.uuid4().hex[:8]}__{filename}"
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
    if not (token and key and name and parts_in and upload_id):
        return jsonify(ok=False, error="Onvolledig afronden (ontbrekende velden)"), 400
    try:
        s3.complete_multipart_upload(
            Bucket=S3_BUCKET, Key=key,
            MultipartUpload={"Parts": sorted(parts_in, key=lambda p: p["PartNumber"])},
            UploadId=upload_id
        )
        head = s3.head_object(Bucket=S3_BUCKET, Key=key)
        size = int(head.get("ContentLength", 0))
        c = db()
        c.execute("""INSERT INTO items(token,s3_key,name,path,size_bytes) VALUES(?,?,?,?,?)""",
                  (token, key, name, path, size))
        c.commit(); c.close()
        return jsonify(ok=True)
    except (ClientError, BotoCoreError):
        log.exception("mpu_complete failed")
        return jsonify(ok=False, error="server_error"), 500

# Download pages/streams
@app.route("/p/<token>", methods=["GET","POST"])
def package_page(token):
    c = db()
    pkg = c.execute("SELECT * FROM packages WHERE token=?", (token,)).fetchone()
    if not pkg: c.close(); abort(404)

    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc):
        rows = c.execute("SELECT s3_key FROM items WHERE token=?", (token,)).fetchall()
        for r in rows:
            try: s3.delete_object(Bucket=S3_BUCKET, Key=r["s3_key"])
            except Exception: pass
        c.execute("DELETE FROM items WHERE token=?", (token,))
        c.execute("DELETE FROM packages WHERE token=?", (token,))
        c.commit(); c.close(); abort(410)

    if pkg["password_hash"]:
        if request.method == "GET" and not session.get(f"allow_{token}", False):
            return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                        <h3>Voer wachtwoord in</h3>
                        <input class="input" type="password" name="password" required>
                        <button class="btn" style="margin-top:.6rem">Ontgrendel</button>
                      </form>"""
        if request.method == "POST":
            if not check_password_hash(pkg["password_hash"], request.form.get("password","")):
                return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                            <h3 style="color:#b91c1c">Onjuist wachtwoord</h3>
                            <input class="input" type="password" name="password" required>
                            <button class="btn" style="margin-top:.6rem">Opnieuw</button>
                          </form>"""
            session[f"allow_{token}"] = True

    items = c.execute("SELECT id,name,path,size_bytes FROM items WHERE token=? ORDER BY path", (token,)).fetchall()
    c.close()

    share_link = url_for("package_page", token=token, _external=True)
    total_bytes = sum(int(r["size_bytes"]) for r in items)
    total_h = human(total_bytes)
    dt = datetime.fromisoformat(pkg["expires_at"]).replace(second=0, microsecond=0)
    expires_h = dt.strftime("%d-%m-%Y %H:%M")

    its = [{"id":r["id"], "name":r["name"], "path":r["path"], "size_h":human(int(r["size_bytes"]))} for r in items]

    return render_template_string(
        PACKAGE_HTML,
        token=token, title=pkg["title"],
        items=its, share_link=share_link, total_human=total_h,
        expires_human=expires_h, base_css=BASE_CSS
    )

@app.route("/file/<token>/<int:item_id>")
def stream_file(token, item_id):
    c = db()
    pkg = c.execute("SELECT * FROM packages WHERE token=?", (token,)).fetchone()
    if not pkg: c.close(); abort(404)
    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc): c.close(); abort(410)
    if pkg["password_hash"] and not session.get(f"allow_{token}", False): c.close(); abort(403)
    it = c.execute("SELECT * FROM items WHERE id=? AND token=?", (item_id, token)).fetchone()
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
        resp.headers["X-Filename"] = it["name"]
        if length: resp.headers["Content-Length"] = str(length)
        return resp
    except Exception:
        log.exception("stream_file failed")
        abort(500)

@app.route("/zip/<token>")
def stream_zip(token):
    # ZIP-precheck + compat voor diverse zipstream-ng API's
    c = db()
    pkg = c.execute("SELECT * FROM packages WHERE token=?", (token,)).fetchone()
    if not pkg: c.close(); abort(404)
    if datetime.fromisoformat(pkg["expires_at"]) <= datetime.now(timezone.utc): c.close(); abort(410)
    if pkg["password_hash"] and not session.get(f"allow_{token}", False): c.close(); abort(403)
    rows = c.execute("SELECT name,path,s3_key FROM items WHERE token=? ORDER BY path", (token,)).fetchall()
    c.close()
    if not rows: abort(404)

    # Precheck: welke items ontbreken?
    missing = []
    try:
      for r in rows:
        try:
          s3.head_object(Bucket=S3_BUCKET, Key=r["s3_key"])
        except ClientError as ce:
          code = ce.response.get("Error", {}).get("Code", "")
          if code in {"NoSuchKey", "NotFound", "404"}:
            missing.append(r["path"] or r["name"])
          else:
            raise
    except Exception:
      log.exception("zip precheck failed")
      resp = Response("ZIP precheck mislukt. Zie serverlogs.", status=500, mimetype="text/plain")
      resp.headers["X-Error"] = "zip_precheck_failed"
      return resp

    if missing:
      text = "De volgende items ontbreken in S3 en kunnen niet gezipt worden:\n- " + "\n- ".join(missing)
      resp = Response(text, mimetype="text/plain", status=422)
      resp.headers["X-Error"] = "NoSuchKey: " + ", ".join(missing)
      return resp

    try:
        z = ZipStream()  # geen kwargs

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

        methods_tried=[]

        def add_compat(arcname, gen_factory):
            if hasattr(z,"add_iter"):
                try:
                    methods_tried.append("add_iter(arcname, iterator)")
                    z.add_iter(arcname, gen_factory()); return
                except Exception: pass
            try:
                methods_tried.append("add(arcname=..., iterable=...)")
                z.add(arcname=arcname, iterable=gen_factory()); return
            except Exception: pass
            try:
                methods_tried.append("add(arcname=..., stream=...)")
                z.add(arcname=arcname, stream=gen_factory()); return
            except Exception: pass
            try:
                methods_tried.append("add(arcname=..., fileobj=...)")
                z.add(arcname=arcname, fileobj=_GenReader(gen_factory())); return
            except Exception: pass
            try:
                methods_tried.append("add(arcname, iterator)")
                z.add(arcname, gen_factory()); return
            except Exception: pass
            try:
                methods_tried.append("add(iterator, arcname)")
                z.add(gen_factory(), arcname); return
            except Exception: pass
            raise RuntimeError("Geen compatibele zipstream-ng add()-signatuur gevonden")

        for r in rows:
            arcname = r["path"] or r["name"]
            def reader(key=r["s3_key"]):
                obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
                for chunk in obj["Body"].iter_chunks(1024*512):
                    if chunk: yield chunk
            add_compat(arcname, lambda: reader())

        def generate():
            for chunk in z: yield chunk

        filename = (pkg["title"] or f"pakket-{token}").strip().replace('"','')
        if not filename.lower().endswith(".zip"): filename += ".zip"

        resp = Response(stream_with_context(generate()), mimetype="application/zip")
        resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        resp.headers["X-Filename"] = filename
        return resp

    except Exception as e:
        log.exception("stream_zip failed")
        msg = f"ZIP generatie mislukte. Tried: {', '.join(methods_tried)}. Err: {e}"
        resp = Response(msg, status=500, mimetype="text/plain")
        resp.headers["X-Error"] = "zipstream_failed"
        return resp

# Contact
EMAIL_RE  = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_RE  = re.compile(r"^[0-9+()\\s-]{8,20}$")
ALLOWED_TB = {0.5, 1.0, 2.0, 5.0}

@app.route("/contact", methods=["GET","POST"])
def contact():
    if request.method == "GET":
        return render_template_string(
            CONTACT_HTML, error=None,
            form={"login_email":"", "storage_tb":"1", "company":"", "phone":""},
            base_css=BASE_CSS
        )
    login_email   = (request.form.get("login_email") or "").strip()
    storage_tb_raw= (request.form.get("storage_tb") or "").strip()
    company       = (request.form.get("company") or "").strip()
    phone         = (request.form.get("phone") or "").strip()

    errors = []
    if not EMAIL_RE.match(login_email): errors.append("Vul een geldig e-mailadres in.")
    try: storage_tb = float(storage_tb_raw.replace(",", ".")); 
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
    price    = price_map.get(storage_tb, "op aanvraag")
    body = (
        "Er is een nieuwe aanvraag binnengekomen:\n\n"
        f"- Gewenste inlog-e-mail: {login_email}\n"
        f"- Gewenste opslag: {storage_tb} TB (indicatie {price})\n"
        f"- Bedrijfsnaam: {company}\n"
        f"- Telefoonnummer: {phone}\n"
    )

    try:
        if smtp_configured():
            msg = EmailMessage()
            msg["Subject"] = subject; msg["From"] = SMTP_FROM; msg["To"] = MAIL_TO
            msg.set_content(body)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
                s.starttls(); s.login(SMTP_USER, SMTP_PASS); s.send_message(msg)
            return render_template_string(CONTACT_DONE_HTML, base_css=BASE_CSS)
    except Exception:
        pass

    from urllib.parse import quote
    mailto = f"mailto:{MAIL_TO}?subject={quote(subject)}&body={quote(body)}"
    return render_template_string(CONTACT_MAIL_FALLBACK_HTML, mailto_link=mailto, base_css=BASE_CSS)

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
    app.run(debug=True)
