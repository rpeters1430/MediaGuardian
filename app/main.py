import hmac
import sqlite3
import os
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse

from . import core

TOKEN = os.getenv("MG_API_TOKEN", "")


@asynccontextmanager
async def lifespan(app):
    if len(TOKEN) < 24:
        raise RuntimeError("MG_API_TOKEN must be at least 24 characters")
    core.init_db()
    yield


app = FastAPI(
    title="MediaGuardian",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def authenticate(x_api_key: str | None = Header(default=None)):
    if not x_api_key or not hmac.compare_digest(x_api_key, TOKEN):
        raise HTTPException(401, detail="Invalid API key")


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready():
    try:
        with core.connect() as db:
            db.execute("SELECT 1")
    except sqlite3.Error:
        raise HTTPException(503, detail="Database unavailable") from None
    return {"status": "ready"}


@app.get("/api/status", dependencies=[Depends(authenticate)])
def status():
    return core.stats()


@app.get("/api/issues", dependencies=[Depends(authenticate)])
def issues(limit: int = 100):
    return core.list_issues(limit)


@app.post("/api/scan", status_code=202, dependencies=[Depends(authenticate)])
def scan():
    if not core.reserve_scan():
        raise HTTPException(409, detail="Scan already running")
    threading.Thread(target=core.scan_once, kwargs={"reserved": True}, daemon=True).start()
    return {"accepted": True}


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediaGuardian</title>
<style>body{font:16px system-ui;background:#101924;color:#eaf1f8;max-width:960px;margin:35px auto;padding:16px}
section{background:#1a2939;padding:20px;border-radius:12px;margin:16px 0}
button,input{padding:10px;border-radius:7px;border:1px solid #607080}
button{cursor:pointer;background:#267ba4;color:white}input{background:#142333;color:white}
li{padding:8px;border-bottom:1px solid #445}</style></head>
<body><h1>MediaGuardian</h1><p>Read-only media inventory and health</p>
<section><label>API token <input id="token" type="password" autocomplete="off"></label>
<button onclick="refresh()">Refresh</button><button onclick="scan()">Scan</button>
<p>Keep this service bound to localhost or use an authenticated TLS proxy.</p></section>
<section><h2>Inventory</h2><div id="stats">Enter your API token.</div></section>
<section><h2>Issues</h2><ul id="issues"></ul></section>
<script>
const token=document.getElementById('token');
async function request(path,method='GET'){
  const response=await fetch(path,{method,headers:{'X-API-Key':token.value}});
  if(!response.ok)throw Error('HTTP '+response.status);
  return response.json();
}
async function refresh(){
  try{
    const [data,issues]=await Promise.all([request('/api/status'),request('/api/issues')]);
    document.getElementById('stats').textContent =
      data.total_files+' media files | '+(data.bytes/1073741824).toFixed(2)+
      ' GiB | '+(data.scan.running?'Scanning':'Idle')+
      ' | '+JSON.stringify(data.statuses)+
      (data.scan.last_error?' | Error: '+data.scan.last_error:'');
    const list=document.getElementById('issues');list.replaceChildren();
    for(const item of issues){
      const li=document.createElement('li');
      li.textContent=item.status+' — '+item.path+' — '+(item.issue||'');
      list.append(li);
    }
  }catch(err){document.getElementById('stats').textContent=err.message;}
}
async function scan(){try{await request('/api/scan','POST');await refresh();}
catch(err){document.getElementById('stats').textContent=err.message;}}
</script></body></html>"""
