#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sqlite3, uuid, tempfile, zipfile, smtplib, re, traceback
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

# ---------------- Config ----------------
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "files.db"

AUTH_EMAIL = "info@oldehanter.nl"
AUTH_PASSWORD = "Hulsmaat"

MAX_RELAY_MB = int(os.environ.get("MAX_RELAY_MB", "5120"))  # 5 GB default
MAX_RELAY_BYTES = MAX_RELAY_MB * 1024 * 1024

# Backblaze B2 (S3-compatibel)
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

# S3 client
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
  --brand:#003366; --brand-2:#0f4c98;
  --text:#0f172a; --muted:#475569; --line:#d1d5db; --ring:#2563eb;
  --surface:#ffffff; --surface-2:#f1f5f9;
}
html,body{height:100%}
body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:var(--text);margin:0;position:relative;overflow-x:hidden}

/* Dynamische, bewegende achtergrond */
.bg{position:fixed;inset:0;z-index:-2;background:
  radial-gradient(60vmax 60vmax at 15% 25%,var(--bg1) 0%,transparent 60%),
  radial-gradient(55vmax 55vmax at 85% 20%,var(--bg2) 0%,transparent 60%),
  radial-gradient(60vmax 60vmax at 50% 90%,var(--bg3) 0%,transparent 60%),
  linear-gradient(180deg,#eef2f7 0%,#e9eef6 100%)}
.bg:before,.bg:after{content:"";position:absolute;inset:-8%;will-change:transform}
.bg:before{background:
  radial-gradient(40% 60% at 20% 30%,rgba(255,255,255,.35),transparent),
  radial-gradient(50% 60% at 80% 25%,rgba(255,255,255,.25),transparent);
  animation:f1 12s linear infinite}
.bg:after{background:
  radial-gradient(35% 50% at 60% 70%,rgba(255,255,255,.22),transparent),
  radial-gradient(45% 55% at 30% 80%,rgba(255,255,255,.18),transparent);
  animation:f2 18s linear infinite}
@keyframes f1{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(1.6%,-1.6%,0) rotate(180deg)}100%{transform:translate3d(0,0,0) rotate(360deg)}}
@keyframes f2{0%{transform:translate3d(0,0,0) rotate(0)}50%{transform:translate3d(-1.4%,1.4%,0) rotate(-180deg)}100%{transform:translate3d(0,0,0) rotate(-360deg)}}

.wrap{max-width:980px;margin:6vh auto;padding:0 1rem}
.card{padding:1.5rem;background:var(--panel);border:1px solid var(--panel-b);
      border-radius:18px;box-shadow:0 18px 40px rgba(0,0,0,.12);backdrop-filter: blur(10px)}
h1{line-height:1.15}

.footer{color:#334155;margin-top:1.2rem;text-align:center}
.small{font-size:.9rem;color:var(--muted)}

/* Uniforme invoervelden */
label{display:block;margin:.65rem 0 .35rem;font-weight:600;color:var(--text)}
.input, input[type=text], input[type=password], input[type=email], input[type=number],
select, textarea{
  width:100%; display:block; appearance:none;
  padding: .85rem 1rem; border-radius:12px; border:1px solid var(--line);
  background:#f0f6ff; color:var(--text);
  outline: none; transition: box-shadow .15s, border-color .15s, background .15s;
}
textarea{min-height:120px; resize:vertical}
input[readonly], .input[readonly]{background:var(--surface-2)}
input:focus, .input:focus, select:focus, textarea:focus{
  border-color: var(--ring);
  box-shadow: 0 0 0 4px rgba(37,99,235,.15);
}
/* File input */
input[type=file]{padding:.55rem 1rem; background:#f0f6ff; cursor:pointer}
input[type=file]::file-selector-button{
  margin-right:.75rem; border:1px solid var(--line);
  background:var(--surface-2); color:var(--text);
  padding:.55rem .9rem; border-radius:10px; cursor:pointer;
}
input[type=file]::file-selector-button:hover{background:#e8edf4}
/* Radio/checkbox */
input[type=radio], input[type=checkbox]{accent-color: var(--brand-2); width:1.05rem;height:1.05rem}
/* Knoppen */
.btn{
  padding:.95rem 1.2rem;border:0;border-radius:12px;
  background:var(--brand);color:#fff;font-weight:700;cursor:pointer;
  box-shadow:0 4px 14px rgba(0,51,102,.25); transition:filter .15s, transform .02s
}
.btn:hover{filter:brightness(1.05)}
.btn:active{transform:translateY(1px)}
.btn.secondary{background:var(--brand-2)}
/* Progress bars */
.progress{height:10px;background:#e5ecf6;border-radius:999px;overflow:hidden;margin-top:.75rem}
.progress > i{display:block;height:100%;width:0;background:linear-gradient(90deg,#0f4c98,#1e90ff);transition:width .1s}
.spinner{display:inline-block;width:18px;height:18px;border:3px solid #cbd5e1;border-top-color:#0f4c98;border-radius:50%;animation:sp 1s linear infinite;margin-left:.4rem}
@keyframes sp{to{transform:rotate(360deg)}}
"""

# -------------- Templates --------------
LOGIN_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Inloggen – Olde Hanter</title>
<style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true"></div>
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
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Olde Hanter – Upload</title>
<style>
{{ base_css }}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:1rem}
h1{margin:.25rem 0 1rem;color:var(--brand);font-size:2.1rem}
.logout a{color:var(--brand);text-decoration:none;font-weight:700}
.toggle{display:flex;gap:.75rem;align-items:center;margin:.4rem 0 .8rem}
.badge{display:none}
</style></head><body>
<div class="bg" aria-hidden="true"></div>

<div class="wrap">
  <div class="topbar">
    <h1>Bestanden delen met Olde Hanter</h1>
    <div class="logout">Ingelogd als {{ user }} • <a href="{{ url_for('logout') }}">Uitloggen</a></div>
  </div>

  <form id="f" class="card" enctype="multipart/form-data" autocomplete="off">
    <label>Uploadtype</label>
    <div class="toggle">
      <label><input type="radio" name="upmode" value="files" checked> Bestand(en)</label>
      <label><input type="radio" name="upmode" value="folder"> Map</label>
      <span class="badge"></span>
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
        <label for="exp">Verloopt over (dagen)</label>
        <input id="exp" class="input" type="number" name="expiry_days" min="1" value="24">
      </div>
      <div>
        <label for="pw">Wachtwoord (optioneel)</label>
        <input id="pw" class="input" type="password" name="password" placeholder="Laat leeg voor geen wachtwoord" autocomplete="new-password" autocapitalize="off" spellcheck="false">
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
  // Toggle bestand/map
  const modeRadios = document.querySelectorAll('input[name="upmode"]');
  const fileRow = document.getElementById('fileRow');
  const folderRow = document.getElementById('folderRow');
  modeRadios.forEach(r => r.addEventListener('change', () => {
    const mode = document.querySelector('input[name="upmode"]:checked').value;
    fileRow.style.display = (mode==='files') ? '' : 'none';
    folderRow.style.display = (mode==='folder') ? '' : 'none';
  }));

  const form = document.getElementById('f');
  const fileInput = document.getElementById('fileInput');
  const folderInput = document.getElementById('folderInput');
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

  // === directUpload met retries, backoff, timeout, 2-way progress ===
  async function directUpload(file, expiryDays, password){
    const CHUNK = 6 * 1024 * 1024; // 6MB parts
    const CONCURRENCY = 2;         // rustiger en stabieler

    // 1) init multipart
    const initRes = await fetch("{{ url_for('mpu_init') }}", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ filename: file.name, contentType: file.type || "application/octet-stream" })
    });
    const init = await initRes.json();
    if(!initRes.ok || !init.ok) throw new Error(init.error || "Init mislukt");
    const { token, key, uploadId } = init;

    const parts = Math.ceil(file.size / CHUNK);
    const partProgress = new Array(parts).fill(0);

    function updateBar(){
      const uploaded = partProgress.reduce((a,b)=>a+b,0);
      const p = Math.round((uploaded / file.size) * 100);
      upbarFill.style.width = Math.min(p,100) + "%";
      uptext.textContent = p < 100 ? (p + "%") : "100% – verwerken…";
    }
    const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));

    async function signPart(partNumber){
      const ps = await fetch("{{ url_for('mpu_sign') }}", {
        method: "POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({ key, uploadId, partNumber })
      });
      const sig = await ps.json();
      if(!ps.ok || !sig.ok) throw new Error(sig.error || "Sign part mislukt");
      return sig.url;
    }

    async function putPartOnce(partNumber, url, blob, idx){
      return await new Promise((resolve,reject)=>{
        const xhr = new XMLHttpRequest();
        xhr.open("PUT", url, true);
        xhr.timeout = 120000; // 120s per part
        xhr.upload.onprogress = (ev)=>{
          if(ev.lengthComputable){
            partProgress[idx] = ev.loaded;
            updateBar();
          }else{
            uptext.textContent = 'Bezig met uploaden…';
          }
        };
        xhr.onload = ()=>{
          if(xhr.status>=200 && xhr.status<300){
            partProgress[idx] = blob.size;
            updateBar();
            const tag = xhr.getResponseHeader("ETag");
            resolve(tag ? tag.replaceAll('"','') : null);
          } else {
            reject(new Error("HTTP "+xhr.status));
          }
        };
        xhr.onerror   = ()=> reject(new Error("Netwerkfout op part "+partNumber));
        xhr.ontimeout = ()=> reject(new Error("Timeout op part "+partNumber));
        xhr.send(blob);
      });
    }

    async function uploadPartWithRetry(partNumber){
      const idx   = partNumber - 1;
      const start = idx * CHUNK;
      const end   = Math.min(start + CHUNK, file.size);
      const blob  = file.slice(start, end);

      const MAX_TRIES = 5;
      for(let attempt=1; attempt<=MAX_TRIES; attempt++){
        try{
          const url = await signPart(partNumber);
          const etag = await putPartOnce(partNumber, url, blob, idx);
          return { PartNumber: partNumber, ETag: etag };
        }catch(err){
          if(attempt === MAX_TRIES){
            throw new Error((err && err.message) ? err.message : ("Part "+partNumber+" gefaald"));
          }
          const backoff = Math.round(400 * Math.pow(2, attempt-1) * (0.85 + Math.random()*0.3));
          await sleep(backoff);
        }
      }
    }

    let next = 1;
    const results = new Array(parts);
    const runners = new Array(CONCURRENCY).fill(0).map(async ()=>{
      while(next <= parts){
        const mine = next++;
        results[mine-1] = await uploadPartWithRetry(mine);
      }
    });
    await Promise.all(runners);

    const finRes = await fetch("{{ url_for('mpu_complete') }}", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({
        token, key, name:file.name, parts: results,
        expiry_days: expiryDays, password: password || ""
      })
    });
    const fin = await finRes.json();
    if(!finRes.ok || !fin.ok) throw new Error(fin.error || "Afronden mislukt");
    return fin.link;
  }

  form.addEventListener('submit', async (e)=>{
    e.preventDefault();
    const files = gatherFiles();
    if(!files || files.length===0){ alert("Kies bestand(en) of map"); return; }

    const expiryDays = document.getElementById('exp').value || '24';
    const password = document.getElementById('pw').value || '';
    const isSingle = (files.length === 1);

    upbar.style.display='block'; uptext.style.display='block';
    upbarFill.style.width='0%'; uptext.textContent='0%';

    try{
      if(isSingle){
        const link = await directUpload(files[0], expiryDays, password);
        upbarFill.style.width='100%'; uptext.textContent='Klaar';
        resBox.innerHTML = `
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
        return;
      }

      // Server-relay voor meerdere bestanden of mappen (zip)
      const fd = new FormData();
      fd.append('expiry_days', expiryDays);
      fd.append('password', password);
      for(const f of files){ fd.append('files', f, f.name); fd.append('paths', relPath(f)); }

      const xhr = new XMLHttpRequest();
      xhr.open('POST', "{{ url_for('upload_relay') }}", true);
      xhr.upload.onprogress = (ev)=>{
        if(ev.lengthComputable){
          const p = Math.round(100*ev.loaded/ev.total);
          upbarFill.style.width = p+'%';
          uptext.textContent = (p<100? p+'%' : '100% – verwerken…');
        }else{
          uptext.textContent = 'Bezig met uploaden…';
        }
      };
      xhr.onreadystatechange = ()=>{
        if(xhr.readyState===4){
          let data = {};
          try{ data = JSON.parse(xhr.responseText||'{}'); }catch(e){}
          if(xhr.status!==200 || !data.ok){
            alert((data && data.error) ? data.error : ('Fout: '+xhr.status));
            return;
          }
          upbarFill.style.width='100%'; uptext.textContent='Klaar';
          resBox.innerHTML = `
            <div class="card" style="margin-top:1rem">
              <strong>Deelbare link</strong>
              <div style="display:flex;gap:.5rem;align-items:center;margin-top:.35rem">
                <input class="input" style="flex:1" value="${data.link}" readonly>
                <button class="btn" type="button"
                  onclick="(navigator.clipboard?.writeText('${data.link}')||Promise.reject()).then(()=>alert('Link gekopieerd'))">
                  Kopieer
                </button>
              </div>
            </div>`;
        }
      };
      xhr.send(fd);

    }catch(err){
      alert(err.message || 'Onbekende fout');
    }
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
input[type=text]{width:100%}
.secondary{background:#0f4c98}
.cta-fixed{
  position:fixed; left:50%; bottom:16px; transform:translateX(-50%);
  z-index:20; padding:1rem 1.25rem; box-shadow:0 10px 24px rgba(0,0,0,.18);
}
@media (max-width:560px){ .cta-fixed{width:calc(100% - 32px); text-align:center} }
</style></head><body>
<div class="bg" aria-hidden="true"></div>

<div class="wrap"><div class="card">
  <h1>Download bestand</h1>
  <div class="meta">
    <div><strong>Bestandsnaam:</strong> {{ name }}</div>
    <div><strong>Grootte:</strong> {{ size_human }}</div>
    <div><strong>Verloopt:</strong> {{ expires_human }}</div>
  </div>

  <button class="btn" id="dlBtn" type="button">Download</button>
  <div class="progress" id="dlbar" style="display:none;margin-top:.6rem"><i></i></div>
  <div class="small" id="dltext" style="display:none">Starten…</div>

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

  <p class="footer" style="margin-bottom:4.5rem">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>

<a class="btn secondary cta-fixed" href="{{ url_for('contact') }}">
  Eigen transfer-oplossing aanvragen
</a>

<script>
  const dlBtn = document.getElementById('dlBtn');
  const dlbar = document.getElementById('dlbar');
  const fill = dlbar.querySelector('i');
  const dltext = document.getElementById('dltext');

  async function downloadWithProgress(){
    dlbar.style.display='block'; dltext.style.display='block';
    fill.style.width='0%'; dltext.textContent='Starten…';

    const res = await fetch("{{ url_for('stream_download', token=token) }}");
    if(!res.ok) { alert('Fout bij starten download ('+res.status+')'); return; }

    const total = parseInt(res.headers.get('Content-Length') || '0', 10);
    const filename = res.headers.get('X-Filename') || '{{ name }}';

    const reader = res.body.getReader();
    const chunks = [];
    let received = 0;

    while(true){
      const {done, value} = await reader.read();
      if(done) break;
      chunks.push(value);
      received += value.length;
      if(total){
        const p = Math.round(received/total*100);
        fill.style.width = p + '%';
        dltext.textContent = p + '%';
      }else{
        dltext.textContent = (received/1024/1024).toFixed(1) + ' MB…';
      }
    }

    const blob = new Blob(chunks);
    fill.style.width='100%'; dltext.textContent='Klaar';
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename;
    document.body.appendChild(a); a.click(); a.remove();
    URL.revokeObjectURL(url);
  }

  dlBtn.addEventListener('click', ()=>{ downloadWithProgress(); });
</script>
</body></html>
"""

CONTACT_HTML = """
<!doctype html><html lang="nl"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Eigen transfer-oplossing – Olde Hanter</title>
<style>{{ base_css }}</style></head><body>
<div class="bg" aria-hidden="true"></div>
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
    <p class="small">Richtprijs wordt op basis van je keuze meegestuurd in de e-mail.</p>
    <button class="btn" type="submit" style="margin-top:1rem">Verstuur aanvraag</button>
  </form>
  <p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p>
</div></div>
</body></html>
"""

CONTACT_DONE_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag verstuurd</title>
<style>{{ base_css }}</style>
</head><body><div class="bg" aria-hidden="true"></div>
<div class="wrap"><div class="card"><h1>Dank je wel!</h1><p>Je aanvraag is verstuurd. We nemen zo snel mogelijk contact met je op.</p><p class="footer">Olde Hanter Bouwconstructies • Bestandentransfer</p></div></div></body></html>
"""

CONTACT_MAIL_FALLBACK_HTML = """
<!doctype html><html><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Aanvraag gereed</title>
<style>{{ base_css }}</style>
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
    return render_template_string(INDEX_HTML, user=session.get("user"), base_css=BASE_CSS)

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

# -------- Multipart Upload (grote single files) --------
@app.route("/mpu-init", methods=["POST"])
def mpu_init():
    if not logged_in():
        return abort(401)
    data = request.get_json(force=True, silent=True) or {}
    filename = secure_filename(data.get("filename") or "")
    content_type = data.get("contentType") or "application/octet-stream"
    if not filename:
        return jsonify(ok=False, error="Geen bestandsnaam"), 400

    token = uuid.uuid4().hex[:10]
    object_key = f"uploads/{token}__{filename}"

    init = s3.create_multipart_upload(
        Bucket=S3_BUCKET, Key=object_key, ContentType=content_type
    )
    upload_id = init["UploadId"]
    return jsonify(ok=True, token=token, key=object_key, uploadId=upload_id)

@app.route("/mpu-sign", methods=["POST"])
def mpu_sign():
    if not logged_in():
        return abort(401)
    data = request.get_json(force=True, silent=True) or {}
    key = data.get("key")
    upload_id = data.get("uploadId")
    part_no = int(data.get("partNumber") or 0)
    if not key or not upload_id or part_no <= 0:
        return jsonify(ok=False, error="Onvolledig mpu-sign verzoek"), 400

    url = s3.generate_presigned_url(
        "upload_part",
        Params={"Bucket": S3_BUCKET, "Key": key, "UploadId": upload_id, "PartNumber": part_no},
        ExpiresIn=3600,
        HttpMethod="PUT",
    )
    return jsonify(ok=True, url=url)

@app.route("/mpu-complete", methods=["POST"])
def mpu_complete():
    if not logged_in():
        return abort(401)
    data = request.get_json(force=True, silent=True) or {}
    token   = data.get("token")
    key     = data.get("key")
    name    = data.get("name")
    parts   = data.get("parts") or []  # [{PartNumber:int, ETag:str}, ...]
    days    = float(data.get("expiry_days") or 24)
    pw      = data.get("password") or ""
    if not token or not key or not name or not parts:
        return jsonify(ok=False, error="Onvolledige complete-data"), 400

    s3.complete_multipart_upload(
        Bucket=S3_BUCKET, Key=key,
        MultipartUpload={"Parts": sorted(parts, key=lambda p: p["PartNumber"])}
    )
    head = s3.head_object(Bucket=S3_BUCKET, Key=key)
    size = int(head.get("ContentLength", 0))

    pw_hash = generate_password_hash(pw) if pw else None
    expires_at = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    c = db()
    c.execute("""INSERT INTO files(token,stored_path,original_name,password_hash,expires_at,size_bytes,created_at)
                 VALUES(?,?,?,?,?,?,?)""",
              (token, key, name, pw_hash, expires_at, size, datetime.now(timezone.utc).isoformat()))
    c.commit(); c.close()

    link = url_for("download", token=token, _external=True)
    return jsonify(ok=True, link=link)

# -------- Server-relay upload (kleine/multi/mapper) --------
@app.route("/upload-relay", methods=["POST"])
def upload_relay():
    if not logged_in():
        return abort(401)

    files = request.files.getlist("files")
    paths = request.form.getlist("paths")
    if not files:
        return jsonify(ok=False, error="Geen bestand"), 400

    content_len = request.content_length or 0
    if content_len and content_len > MAX_RELAY_BYTES:
        return jsonify(ok=False, error=f"Upload groter dan {MAX_RELAY_MB} MB"), 413

    expiry_days = float(request.form.get("expiry_days") or 24)
    password = request.form.get("password") or ""
    pw_hash = generate_password_hash(password) if password else None

    must_zip = len(files) > 1 or any("/" in (p or "") for p in paths)
    token = uuid.uuid4().hex[:10]

    try:
        if must_zip:
            root = None
            for p in paths:
                if "/" in p:
                    root = p.split("/", 1)[0]
                    break
            zip_name = f"{(root or 'map-upload')}_{token}.zip"
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

    except Exception as e:
        print("UPLOAD ERROR:", e)
        traceback.print_exc()
        return jsonify(ok=False, error="Upload verwerken mislukt. Probeer opnieuw of neem contact op."), 500

# -------- Download scherm --------
@app.route("/d/<token>", methods=["GET","POST"])
def download(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row: abort(404)

    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        try: s3.delete_object(Bucket=S3_BUCKET, Key=row["stored_path"])
        except: pass
        c = db(); c.execute("DELETE FROM files WHERE token=?", (token,)); c.commit(); c.close()
        abort(410)

    # Wachtwoord-check
    if row["password_hash"]:
        if request.method == "GET":
            return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                        <h3>Voer wachtwoord in</h3>
                        <input class="input" type="password" name="password" required>
                        <button class="btn" style="margin-top:.6rem">Ontgrendel</button>
                      </form>"""
        if not check_password_hash(row["password_hash"], request.form.get("password","")):
            return """<form method="post" style="max-width:420px;margin:4rem auto;font-family:system-ui">
                        <h3 style="color:#b91c1c">Onjuist wachtwoord</h3>
                        <input class="input" type="password" name="password" required>
                        <button class="btn" style="margin-top:.6rem">Opnieuw</button>
                      </form>"""
        session[f"allow_{token}"] = True

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

# -------- Streaming download met echte progress --------
@app.route("/stream/<token>")
def stream_download(token):
    c = db(); row = c.execute("SELECT * FROM files WHERE token=?", (token,)).fetchone(); c.close()
    if not row: abort(404)
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc): abort(410)
    if row["password_hash"] and not session.get(f"allow_{token}", False): abort(403)

    head = s3.head_object(Bucket=S3_BUCKET, Key=row["stored_path"])
    length = int(head.get("ContentLength", 0))
    obj = s3.get_object(Bucket=S3_BUCKET, Key=row["stored_path"])

    def gen():
        for chunk in obj["Body"].iter_chunks(1024 * 512):
            if chunk: yield chunk

    filename = row["original_name"]
    resp = Response(stream_with_context(gen()), mimetype="application/octet-stream")
    if length: resp.headers["Content-Length"] = str(length)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.headers["X-Filename"] = filename
    return resp

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

# Healthcheck
@app.route("/health-s3")
def health():
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        return {"ok": True, "bucket": S3_BUCKET}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500

if __name__ == "__main__":
    app.run(debug=True)
