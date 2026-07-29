"""Onboard appliance control panel — a small FastAPI service (default :4267).

Lets you, from a browser on the boat:
  - tune the POI window (±s), the sample rate (Hz) and the `needed` channel set — written to a
    hot-reloaded config the replicator picks up live (no restart);
  - see the stack status + egress queue depth;
  - update the toolkit from GitHub (git pull + rebuild the worker containers);
  - restart a service.

It talks to Docker via the mounted socket, so it can (re)build/restart the appliance. Keep it on a
trusted LAN — the Docker socket is root-equivalent on the host.
"""
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/appliance.json"))
SPOOL_DIR = Path(os.getenv("SPOOL_DIR", "/spool"))
COMPOSE_FILE = os.getenv("COMPOSE_FILE", "")           # host path to docker-compose.yml
TOOLKIT_DIR = os.getenv("TOOLKIT_DIR", "")             # host path to the sailing-data-toolkit repo
BOAT = os.getenv("ONBOARD_BOAT", "B3")
DEFAULT_CHANNELS_FILE = Path(os.getenv("DEFAULT_CHANNELS_FILE", "/app/default_channels.json"))
UPDATE_LOG = Path("/tmp/update.log")
WORKER_SERVICES = ["bridge", "detection", "replicator"]

app = FastAPI(title="Onboard Appliance Control")
_update_lock = threading.Lock()


# ---------------------------------------------------------------- config ----

def _default_channels() -> list:
    try:
        return json.loads(DEFAULT_CHANNELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 0, "window_s": 30, "downsample_ms": 200, "needed_channels": _default_channels()}


def _write_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)  # atomic — replicator never sees a half-written file


@app.on_event("startup")
def _seed_config():
    if not CONFIG_PATH.exists():
        _write_config({
            "version": 1,
            "gcp_host": os.getenv("SIM_HOST", "192.168.100.11"),
            "gcp_port": int(os.getenv("SIM_PORT", "8081")),
            "window_s": 30,
            "downsample_ms": 200,
            "needed_channels": _default_channels(),
        })


class ConfigIn(BaseModel):
    window_s: float
    hz: float                      # UI works in Hz; stored as downsample_ms
    gcp_host: str | None = None
    gcp_port: int | None = None
    needed_channels: list[str] | None = None
    all_channels: bool = False     # when true, ship every channel in the window


@app.get("/api/config")
def get_config():
    cfg = _load_config()
    ds = cfg.get("downsample_ms", 0) or 0
    cfg["hz"] = round(1000.0 / ds, 3) if ds > 0 else 0
    return cfg


@app.post("/api/config")
def set_config(body: ConfigIn):
    if body.window_s <= 0:
        raise HTTPException(400, "window_s must be > 0")
    downsample_ms = 0 if body.hz <= 0 else int(round(1000.0 / body.hz))
    channels = None if body.all_channels else (body.needed_channels or [])
    cfg = _load_config()
    old_host, old_port = cfg.get("gcp_host"), cfg.get("gcp_port")
    new_host = (body.gcp_host or old_host or "").strip() or old_host
    new_port = int(body.gcp_port) if body.gcp_port else old_port
    cfg.update(
        version=int(cfg.get("version", 0)) + 1,
        gcp_host=new_host,
        gcp_port=new_port,
        window_s=float(body.window_s),
        downsample_ms=downsample_ms,
        needed_channels=channels,
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )
    _write_config(cfg)

    # window / Hz / channels hot-reload in the replicator; the GCP source only takes effect on a
    # bridge restart. Do it in the background so the save returns immediately.
    bridge_restarted = False
    if (new_host, new_port) != (old_host, old_port):
        threading.Thread(target=lambda: _compose("restart", "bridge", timeout=120), daemon=True).start()
        bridge_restarted = True

    return {"ok": True, "version": cfg["version"], "downsample_ms": downsample_ms,
            "channels": "all" if channels is None else len(channels),
            "gcp": f"{new_host}:{new_port}", "bridge_restarted": bridge_restarted}


# ---------------------------------------------------------------- docker ----

def _compose(*args, timeout=60) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose"]
    if COMPOSE_FILE:
        cmd += ["-f", COMPOSE_FILE]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@app.get("/api/status")
def status():
    out = {"boat": BOAT, "config": get_config()}
    # containers
    try:
        r = _compose("ps", "--format", "json")
        rows = []
        for line in r.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                rows.append({"name": d.get("Service") or d.get("Name"), "state": d.get("State"), "status": d.get("Status")})
            except Exception:
                pass
        out["services"] = rows
    except Exception as e:
        out["services_error"] = str(e)
    # egress queue depth
    q = SPOOL_DIR / "manoeuvres.jsonl"
    cur = SPOOL_DIR / f"replicator_cursor_{BOAT}.json"
    try:
        total_lines = sum(1 for _ in q.open("r", encoding="utf-8")) if q.exists() else 0
        size = q.stat().st_size if q.exists() else 0
        offset = json.loads(cur.read_text()).get("offset", 0) if cur.exists() else 0
        out["egress"] = {"queued_manoeuvres": total_lines, "bytes_total": size, "bytes_shipped": offset,
                         "pending": max(size - offset, 0)}
    except Exception as e:
        out["egress_error"] = str(e)
    return out


@app.post("/api/restart/{service}")
def restart(service: str):
    if service not in WORKER_SERVICES + ["influxdb", "shore-influxdb"]:
        raise HTTPException(400, f"unknown service {service}")
    r = _compose("restart", service, timeout=120)
    return {"ok": r.returncode == 0, "stdout": r.stdout, "stderr": r.stderr}


# ---------------------------------------------------------------- update ----

def _run_update():
    def log(msg):
        with UPDATE_LOG.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")

    def stream(cmd, **kw):
        log(f"$ {' '.join(cmd)}")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
        for line in p.stdout:
            log(line)
        p.wait()
        log(f"[exit {p.returncode}]")
        return p.returncode

    try:
        UPDATE_LOG.write_text(f"# update started {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        if not TOOLKIT_DIR or not COMPOSE_FILE:
            log("TOOLKIT_DIR / COMPOSE_FILE not set — cannot update from here.")
            return
        if stream(["git", "-C", TOOLKIT_DIR, "pull", "--ff-only"]) != 0:
            log("git pull failed — aborting (no rebuild).")
            return
        # rebuild + recreate only the worker containers (leave control/influx running)
        stream(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"] + WORKER_SERVICES)
        log("# update done")
    except Exception as e:
        log(f"update crashed: {e}")
    finally:
        if _update_lock.locked():
            _update_lock.release()


@app.post("/api/update")
def update():
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(409, "an update is already running")
    threading.Thread(target=_run_update, daemon=True).start()
    return {"started": True}


@app.get("/api/update/log", response_class=PlainTextResponse)
def update_log():
    return UPDATE_LOG.read_text(encoding="utf-8") if UPDATE_LOG.exists() else "(no update run yet)"


# ---------------------------------------------------------------- page ------

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


HTML_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Onboard Appliance — Control</title>
<style>
 :root{color-scheme:light dark}
 *{box-sizing:border-box}
 body{font:14px/1.5 system-ui,sans-serif;margin:0;background:Canvas;color:CanvasText}
 header{padding:16px 20px;border-bottom:1px solid #8884;display:flex;align-items:center;gap:12px}
 header h1{font-size:16px;margin:0}
 .badge{font-size:12px;padding:2px 8px;border:1px solid #8886;border-radius:999px}
 main{max-width:900px;margin:0 auto;padding:20px;display:grid;gap:20px}
 .card{border:1px solid #8884;border-radius:12px;padding:16px}
 .card h2{margin:0 0 12px;font-size:14px;text-transform:uppercase;letter-spacing:.04em;opacity:.7}
 label{display:block;font-weight:600;margin:10px 0 4px}
 input,textarea{width:100%;padding:8px 10px;border:1px solid #8886;border-radius:8px;background:Field;color:FieldText;font:inherit}
 textarea{min-height:120px;font-family:ui-monospace,monospace;font-size:12px}
 .row{display:flex;gap:16px;flex-wrap:wrap}
 .row>div{flex:1;min-width:120px}
 button{padding:9px 14px;border:0;border-radius:8px;background:#2563eb;color:#fff;font:inherit;font-weight:600;cursor:pointer}
 button.ghost{background:#8883;color:CanvasText}
 button:disabled{opacity:.5;cursor:default}
 .muted{opacity:.65}
 .ok{color:#16a34a}.err{color:#dc2626}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{text-align:left;padding:6px 8px;border-bottom:1px solid #8883}
 pre{background:#8881;padding:10px;border-radius:8px;max-height:260px;overflow:auto;font-size:12px}
 .flash{font-size:13px;min-height:18px}
</style></head><body>
<header><h1>⚓ Onboard Appliance</h1><span class="badge" id="boat">B3</span><span class="badge muted" id="ver"></span></header>
<main>
 <section class="card">
  <h2>Telemetry source (GCP / DataViewer)</h2>
  <div class="row">
   <div><label>Host / IP</label><input id="gcp_host" placeholder="192.168.100.11"/></div>
   <div><label>Port</label><input id="gcp_port" type="number" min="1" placeholder="8081"/></div>
  </div>
  <p class="muted" style="margin:8px 0 0">Changing the source restarts the bridge automatically. Real boat = <code>192.168.100.11:8081</code> · sim = <code>192.168.20.13:8085</code>.</p>
 </section>

 <section class="card">
  <h2>Egress parameters (live)</h2>
  <div class="row">
   <div><label>POI window (± seconds)</label><input id="window" type="number" min="1" step="1"/></div>
   <div><label>Sample rate (Hz, 0 = native)</label><input id="hz" type="number" min="0" step="0.5"/></div>
  </div>
  <label>Needed channels (one per line — empty = all channels in window)</label>
  <textarea id="channels" placeholder="Boat.TWA&#10;Boat.TWS_kts&#10;..."></textarea>
  <div class="row" style="margin-top:12px;align-items:center">
   <button onclick="saveConfig()">Save (applies live)</button>
   <span class="flash" id="cfgflash"></span>
  </div>
 </section>

 <section class="card">
  <h2>Status</h2>
  <table id="svc"><tbody></tbody></table>
  <div id="egress" class="muted" style="margin-top:10px"></div>
  <div style="margin-top:12px" class="row">
   <button class="ghost" onclick="refresh()">Refresh</button>
   <button class="ghost" onclick="restart('replicator')">Restart replicator</button>
   <button class="ghost" onclick="restart('detection')">Restart detection</button>
   <button class="ghost" onclick="restart('bridge')">Restart bridge</button>
  </div>
 </section>

 <section class="card">
  <h2>Update toolkit from GitHub</h2>
  <p class="muted">Runs <code>git pull</code> then rebuilds the worker containers (bridge, detection, replicator). Influx and this panel keep running.</p>
  <button id="updbtn" onclick="doUpdate()">Pull &amp; rebuild</button>
  <span class="flash" id="updflash"></span>
  <pre id="updlog" style="display:none"></pre>
 </section>
</main>
<script>
const $=id=>document.getElementById(id);
async function refresh(){
 const s=await (await fetch('./api/status')).json();
 $('boat').textContent=s.boat; $('ver').textContent='config v'+(s.config.version??'?');
 const tb=$('svc').querySelector('tbody'); tb.innerHTML='';
 (s.services||[]).forEach(x=>{const st=(x.state||'').includes('running')?'ok':'err';
  tb.insertAdjacentHTML('beforeend',`<tr><td>${x.name}</td><td class="${st}">${x.state||''}</td><td class="muted">${x.status||''}</td></tr>`);});
 const e=s.egress; if(e) $('egress').textContent=`egress queue: ${e.queued_manoeuvres} manoeuvres · ${e.pending} bytes pending / ${e.bytes_total} total`;
}
async function loadCfg(){
 const c=await (await fetch('./api/config')).json();
 $('gcp_host').value=c.gcp_host||''; $('gcp_port').value=c.gcp_port||'';
 $('window').value=c.window_s; $('hz').value=c.hz;
 $('channels').value=(c.needed_channels===null||c.needed_channels===undefined)?'':c.needed_channels.join('\\n');
}
async function saveConfig(){
 const lines=$('channels').value.split('\\n').map(s=>s.trim()).filter(Boolean);
 const body={gcp_host:$('gcp_host').value.trim(),gcp_port:parseInt($('gcp_port').value)||null,
  window_s:parseFloat($('window').value),hz:parseFloat($('hz').value),
  all_channels:lines.length===0,needed_channels:lines};
 const f=$('cfgflash'); f.textContent='saving…'; f.className='flash';
 const r=await fetch('./api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
 const j=await r.json();
 if(r.ok){f.textContent=`saved · v${j.version} · source ${j.gcp}${j.bridge_restarted?' (bridge restarted)':''} · ${j.channels==='all'?'all channels':j.channels+' channels'} · downsample ${j.downsample_ms||0}ms`;f.className='flash ok';refresh();}
 else{f.textContent='error: '+(j.detail||r.status);f.className='flash err';}
}
async function restart(svc){const f=$('cfgflash');f.textContent='restarting '+svc+'…';
 const r=await fetch('./api/restart/'+svc,{method:'POST'});f.textContent=(r.ok?'restarted ':'error ')+svc;setTimeout(refresh,1500);}
let pollT=null;
async function doUpdate(){
 $('updbtn').disabled=true;$('updflash').textContent='starting…';$('updlog').style.display='block';
 const r=await fetch('./api/update',{method:'POST'});
 if(!r.ok){$('updflash').textContent='error: '+r.status;$('updbtn').disabled=false;return;}
 $('updflash').textContent='running — this rebuilds images, can take minutes';
 clearInterval(pollT); pollT=setInterval(async()=>{
  const t=await (await fetch('./api/update/log')).text(); $('updlog').textContent=t; $('updlog').scrollTop=$('updlog').scrollHeight;
  if(t.includes('# update done')||t.includes('aborting')||t.includes('crashed')){clearInterval(pollT);$('updbtn').disabled=false;$('updflash').textContent='finished';refresh();}
 },2000);
}
loadCfg();refresh();setInterval(refresh,8000);
</script></body></html>"""
