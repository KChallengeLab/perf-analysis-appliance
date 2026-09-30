"""Onboard Data Analysis Console — l'unique interface de gestion de la carte (defaut :4267).

Une seule page pour operer le transmetteur depuis un navigateur, sur un poste du bord :
  - allumer / eteindre / redemarrer la chaine, service par service ou d'un bloc ;
  - suivre en direct les ressources, la bande passante et les logs (pousses par WebSocket) ;
  - regler la source GCP, la fenetre POI, la frequence et la liste des channels `needed`
    (ecrits dans un config relu a chaud : le replicator les prend sans redemarrage) ;
  - mettre a jour la bibliotheque sailing-data-toolkit depuis GitHub et reconstruire.

La console parle au demon Docker via le socket monte : elle est root-equivalente sur l'hote.
A garder sur un LAN de confiance.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, List, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

CONFIG_PATH = Path(os.getenv("CONFIG_PATH", "/config/appliance.json"))
SPOOL_DIR = Path(os.getenv("SPOOL_DIR", "/spool"))
COMPOSE_FILE = os.getenv("COMPOSE_FILE", "")
TOOLKIT_DIR = os.getenv("TOOLKIT_DIR", "")
BOAT = os.getenv("ONBOARD_BOAT", "B3")
PROJECT = os.getenv("COMPOSE_PROJECT", "perf-analysis-appliance")
DEFAULT_CHANNELS_FILE = Path(os.getenv("DEFAULT_CHANNELS_FILE", "/app/default_channels.json"))
UPDATE_LOG = Path("/tmp/update.log")

# La console est elle-meme un service du compose : elle ne doit JAMAIS s'inclure dans un
# arret groupe, sinon plus rien ne peut la rallumer depuis le navigateur.
PIPELINE = ["influxdb", "bridge", "detection", "replicator"]
WORKERS = ["bridge", "detection", "replicator"]
SELF = "control"

SAMPLE_S = float(os.getenv("SAMPLE_S", "2"))
HISTORY = int(os.getenv("HISTORY", "300"))
LOG_LINES = int(os.getenv("LOG_LINES", "600"))

APP_DIR = Path(__file__).parent
app = FastAPI(title="Onboard Data Analysis Console")
_FONTS = APP_DIR / "fonts"
if _FONTS.is_dir():
    app.mount("/fonts", StaticFiles(directory=str(_FONTS)), name="fonts")
_update_lock = threading.Lock()

history: Deque[dict] = deque(maxlen=HISTORY)
logs: Deque[dict] = deque(maxlen=LOG_LINES)
clients: set = set()
_prev_net: Dict[str, tuple] = {}
_prev_at: Optional[float] = None


# ── outils ──────────────────────────────────────────────────────────────────

def _run(cmd: List[str], timeout: float = 20) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _compose(*args, timeout=120) -> subprocess.CompletedProcess:
    cmd = ["docker", "compose"]
    if COMPOSE_FILE:
        cmd += ["-f", COMPOSE_FILE]
    return _run(cmd + list(args), timeout=timeout)


_UNITS = {"b": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}
_SIZE_RE = re.compile(r"([0-9.]+)\s*([a-zA-Z]+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_LEVEL = re.compile(r"\b(ERROR|WARNING|INFO|DEBUG)\b")


def _size(text: str) -> float:
    m = _SIZE_RE.search(text or "")
    if not m:
        return 0.0
    try:
        return float(m.group(1)) * _UNITS.get(m.group(2).lower(), 1)
    except ValueError:
        return 0.0


def _short(name: str) -> str:
    return re.sub(r"-\d+$", "", name.removeprefix(f"{PROJECT}-"))


# ── config ──────────────────────────────────────────────────────────────────

def _default_channels() -> list:
    try:
        return json.loads(DEFAULT_CHANNELS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 0, "window_s": 30, "downsample_ms": 100, "needed_channels": _default_channels()}


def _write_config(cfg: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)  # atomique — le replicator ne voit jamais un fichier a moitie ecrit


class ConfigIn(BaseModel):
    window_s: float
    hz: float
    gcp_host: str | None = None
    gcp_port: int | None = None
    needed_channels: list[str] | None = None
    all_channels: bool = False


@app.get("/api/config")
def get_config():
    cfg = _load_config()
    ds = int(cfg.get("downsample_ms") or 0)
    cfg["hz"] = round(1000 / ds, 3) if ds else 0
    return cfg


@app.post("/api/config")
def set_config(body: ConfigIn):
    cfg = _load_config()
    prev_src = (cfg.get("gcp_host"), cfg.get("gcp_port"))
    cfg["window_s"] = max(1, int(body.window_s))
    cfg["downsample_ms"] = int(round(1000 / body.hz)) if body.hz and body.hz > 0 else 0
    if body.gcp_host:
        cfg["gcp_host"] = body.gcp_host.strip()
    if body.gcp_port:
        cfg["gcp_port"] = int(body.gcp_port)
    cfg["needed_channels"] = [] if body.all_channels else (body.needed_channels or cfg.get("needed_channels") or [])
    cfg["version"] = int(cfg.get("version", 0)) + 1
    _write_config(cfg)
    # La fenetre / la frequence / les channels sont relus a chaud. La source GCP, elle, n'est lue
    # qu'au demarrage du bridge : on le redemarre uniquement si elle a change.
    restarted = False
    if (cfg.get("gcp_host"), cfg.get("gcp_port")) != prev_src:
        _compose("restart", "bridge")
        restarted = True
    return {"ok": True, "version": cfg["version"], "bridge_restarted": restarted}


# ── catalogue de channels (depuis le shore) ─────────────────────────────────
# Le catalogue vient de l'editeur de channels du shore (perf-nav :4444), seule source de
# verite de ce que l'equipe suit. On le recupere COTE SERVEUR : le navigateur du bord n'a
# pas forcement de route vers le shore, il faudrait du CORS, et le token n'a rien a faire
# dans une page. Le resultat est mis en cache sur la carte pour que la page reste utilisable
# quand le lien est coupe — c'est-a-dire la plupart du temps en mer.

SHORE_CHANNELS_URL = os.getenv("SHORE_CHANNELS_URL", "")
SHORE_CHANNELS_TOKEN = os.getenv("SHORE_CHANNELS_TOKEN", "")
CATALOG_PATH = CONFIG_PATH.parent / "shore_channels.json"


def _load_catalog() -> dict:
    try:
        return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


@app.get("/api/channels/catalog")
def channels_catalog():
    """Catalogue connu + selection courante, pour la page de selection.

    Le catalogue affiche est l'union du dernier catalogue shore et de la selection actuelle :
    un channel deja selectionne mais absent du catalogue doit rester visible et decochables,
    sinon on ne pourrait plus l'enlever.
    """
    cat = _load_catalog()
    selected = _load_config().get("needed_channels") or []
    known = set(cat.get("channels") or [])
    tables = dict(cat.get("tables") or {})
    extra = sorted(set(selected) - known)
    if extra:
        tables["(hors catalogue shore)"] = extra
    return {
        "tables": tables,
        "channels": sorted(known | set(selected)),
        "selected": selected,
        "fetched_at": cat.get("fetched_at"),
        "source": cat.get("source"),
        "configured": bool(SHORE_CHANNELS_URL),
    }


@app.post("/api/channels/refresh")
def channels_refresh():
    """Va relire le filtre actif sur le shore et le met en cache localement."""
    if not SHORE_CHANNELS_URL:
        raise HTTPException(400, "SHORE_CHANNELS_URL n'est pas configure sur la carte")
    url = SHORE_CHANNELS_URL
    if SHORE_CHANNELS_TOKEN:
        url += ("&" if "?" in url else "?") + "token=" + SHORE_CHANNELS_TOKEN
    try:
        import urllib.request
        with urllib.request.urlopen(url, timeout=30) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        # Message actionnable : c'est presque toujours le lien ou le token.
        raise HTTPException(502, f"shore injoignable ou reponse invalide : {e}")
    if not isinstance(payload.get("channels"), list):
        raise HTTPException(502, "reponse du shore inattendue (pas de liste 'channels')")
    cat = {
        "channels": payload["channels"],
        "tables": payload.get("tables") or {},
        "count": len(payload["channels"]),
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": SHORE_CHANNELS_URL,
    }
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cat), encoding="utf-8")
    tmp.replace(CATALOG_PATH)
    return {"ok": True, "count": cat["count"], "tables": len(cat["tables"]), "fetched_at": cat["fetched_at"]}


@app.get("/channels", response_class=HTMLResponse)
def channels_page():
    return (APP_DIR / "channels.html").read_text(encoding="utf-8")


@app.get("/console.css")
def console_css():
    return Response((APP_DIR / "console.css").read_text(encoding="utf-8"), media_type="text/css")


# ── etat + pilotage ─────────────────────────────────────────────────────────

def _services() -> List[dict]:
    r = _compose("ps", "-a", "--format", "json", timeout=30)
    rows = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            rows.append({"name": d.get("Service") or d.get("Name"),
                         "state": (d.get("State") or "").lower(),
                         "status": d.get("Status", "")})
        except Exception:
            pass
    return rows


def _pipeline_state() -> dict:
    q = SPOOL_DIR / "manoeuvres.jsonl"
    cur = SPOOL_DIR / f"replicator_cursor_{BOAT}.json"
    sent = SPOOL_DIR / f"replicator_sent_{BOAT}.json"
    queued = size = offset = ranges = 0
    try:
        if q.exists():
            queued = sum(1 for _ in q.open("r", encoding="utf-8"))
            size = q.stat().st_size
        if cur.exists():
            offset = int(json.loads(cur.read_text()).get("offset", 0))
        if sent.exists():
            ranges = len(json.loads(sent.read_text()).get("ranges", []))
    except Exception:
        pass
    return {"queued": queued, "pending_bytes": max(0, size - offset), "sent_ranges": ranges}


def _system() -> dict:
    info: Dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k] = int(v.strip().split()[0]) * 1024
    except Exception:
        pass
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    swt, swf = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    du = shutil.disk_usage("/spool") if SPOOL_DIR.exists() else None
    return {"mem_total": total, "mem_used": total - avail,
            "swap_total": swt, "swap_used": swt - swf,
            "disk_total": du.total if du else 0, "disk_used": du.used if du else 0}


def _stats() -> Dict[str, dict]:
    r = _run(["docker", "stats", "--no-stream", "--format",
              "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.NetIO}}"], timeout=25)
    out: Dict[str, dict] = {}
    for line in r.stdout.strip().splitlines():
        p = line.split("|")
        if len(p) < 5:
            continue
        used, _, limit = p[2].partition(" / ")
        rx, _, tx = p[4].partition(" / ")
        out[_short(p[0])] = {"cpu": float(p[1].rstrip("%") or 0), "mem": _size(used),
                             "mem_limit": _size(limit), "mem_pct": float(p[3].rstrip("%") or 0),
                             "rx": _size(rx), "tx": _size(tx)}
    return out


def _sample() -> dict:
    global _prev_at
    now = time.time()
    st = _stats()
    dt = (now - _prev_at) if _prev_at else None
    rates = {}
    for svc, s in st.items():
        prev = _prev_net.get(svc)
        if prev and dt and dt > 0:
            # Les compteurs repartent de zero quand un conteneur redemarre : delta negatif ignore.
            rates[svc] = {"rx_s": max(0.0, s["rx"] - prev[0]) / dt, "tx_s": max(0.0, s["tx"] - prev[1]) / dt}
        else:
            rates[svc] = {"rx_s": 0.0, "tx_s": 0.0}
        _prev_net[svc] = (s["rx"], s["tx"])
    _prev_at = now
    return {"t": now, "containers": st, "rates": rates, "system": _system(),
            "pipeline": _pipeline_state(), "services": _services()}


@app.get("/api/status")
def status():
    return {"boat": BOAT, "config": get_config(), "services": _services(),
            "pipeline": _pipeline_state(), "toolkit": toolkit_version()}


class PowerIn(BaseModel):
    action: str                      # start | stop | restart
    service: str | None = None       # None = toute la chaine (console exclue)


@app.post("/api/power")
def power(body: PowerIn):
    if body.action not in ("start", "stop", "restart"):
        raise HTTPException(400, "action inconnue")
    if body.service:
        if body.service == SELF:
            raise HTTPException(400, "la console ne peut pas se piloter elle-meme")
        if body.service not in PIPELINE:
            raise HTTPException(400, f"service inconnu : {body.service}")
        targets = [body.service]
    else:
        targets = PIPELINE
    # `up -d` plutot que `start` : recree un conteneur supprime au lieu d'echouer.
    args = (["up", "-d"] + targets) if body.action == "start" else ([body.action] + targets)
    r = _compose(*args, timeout=240)
    return {"ok": r.returncode == 0, "targets": targets,
            "stdout": r.stdout[-4000:], "stderr": r.stderr[-4000:]}


# ── version + mise a jour ───────────────────────────────────────────────────

def _git(*args, cwd: str) -> str:
    try:
        r = _run(["git", "-C", cwd] + list(args), timeout=25)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


@app.get("/api/version")
def toolkit_version():
    if not TOOLKIT_DIR or not os.path.isdir(TOOLKIT_DIR):
        return {"available": False, "reason": "TOOLKIT_DIR absent"}
    dirty = _git("status", "--porcelain", cwd=TOOLKIT_DIR)
    behind = ""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=TOOLKIT_DIR)
    if branch:
        cnt = _git("rev-list", "--count", f"HEAD..origin/{branch}", cwd=TOOLKIT_DIR)
        behind = cnt or ""
    return {
        "available": True,
        "branch": branch,
        "commit": _git("rev-parse", "--short", "HEAD", cwd=TOOLKIT_DIR),
        "subject": _git("log", "-1", "--pretty=%s", cwd=TOOLKIT_DIR),
        "date": _git("log", "-1", "--pretty=%ci", cwd=TOOLKIT_DIR),
        "dirty_files": len([l for l in dirty.splitlines() if l.strip()]),
        "behind": behind,
        "update_blocked": (lambda t: None if t[0] else t[1])(_update_is_safe()),
    }


def _update_is_safe() -> tuple[bool, str]:
    """Le suivi de la branche distante ferait-il perdre des commits locaux ?

    Renvoie (True, "") quand la remise a jour est une avance rapide. Sinon (False, raison) :
    la branche distante ne contient pas le HEAD local, donc s'y aligner ecraserait du travail.
    """
    if not TOOLKIT_DIR or not os.path.isdir(TOOLKIT_DIR):
        return False, "TOOLKIT_DIR absent"
    branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=TOOLKIT_DIR)
    if not branch or branch == "HEAD":
        return False, "le sous-module n'est sur aucune branche (HEAD detache)"
    try:
        r = _run(["git", "-C", TOOLKIT_DIR, "merge-base", "--is-ancestor", "HEAD", f"origin/{branch}"], timeout=25)
    except Exception as e:
        return False, f"impossible de comparer a origin/{branch} ({e})"
    if r.returncode == 0:
        return True, ""
    lost = _git("rev-list", "--count", f"origin/{branch}..HEAD", cwd=TOOLKIT_DIR) or "?"
    return False, (f"origin/{branch} ne contient pas le HEAD local — {lost} commit(s) locaux seraient "
                   f"perdus (branche rebasee ou non poussee ?)")


def _run_update(fetch_only: bool = False):
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
        UPDATE_LOG.write_text(f"# mise a jour demarree {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8")
        project = os.path.dirname(COMPOSE_FILE) if COMPOSE_FILE else ""
        if not project or not os.path.isdir(project):
            log("COMPOSE_FILE absent — impossible de mettre a jour depuis ici.")
            return
        before = _git("rev-parse", "--short", "HEAD", cwd=TOOLKIT_DIR) if TOOLKIT_DIR else "?"
        log(f"# commit toolkit avant : {before}")

        # Garde-fou : ne jamais reculer. `submodule update --remote` suit la branche distante sans
        # se soucier de ce qu'il ecrase — si la branche locale a ete rebasee sans etre poussee,
        # l'operation ramene le toolkit AVANT le rebase et perd du travail en silence.
        ok, why = _update_is_safe()
        if not ok:
            log(f"!! mise a jour refusee : {why}")
            log("   Rien n'a ete touche. Resoudre la divergence (pousser la branche locale, ou")
            log("   reinitialiser volontairement le sous-module) avant de relancer.")
            return

        if stream(["git", "-C", project, "submodule", "update", "--remote", "--init", "sailing-data-toolkit"]) != 0:
            log("!! echec du submodule update — aucune reconstruction. Verifie les modifications locales "
                "(git -C sailing-data-toolkit status) et les droits du depot.")
            return
        after = _git("rev-parse", "--short", "HEAD", cwd=TOOLKIT_DIR) if TOOLKIT_DIR else "?"
        log(f"# commit toolkit apres : {after}")
        if after == before:
            log("# deja a jour — rien a reconstruire.")
            return
        if fetch_only:
            log("# recuperation seule demandee : pas de reconstruction.")
            return
        stream(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"] + WORKERS)
        log("# mise a jour terminee")
    except Exception as e:
        log(f"la mise a jour a plante : {e}")
    finally:
        if _update_lock.locked():
            _update_lock.release()


class UpdateIn(BaseModel):
    fetch_only: bool = False


@app.post("/api/update")
def update(body: UpdateIn | None = None):
    if not _update_lock.acquire(blocking=False):
        raise HTTPException(409, "une mise a jour est deja en cours")
    threading.Thread(target=_run_update, kwargs={"fetch_only": bool(body and body.fetch_only)}, daemon=True).start()
    return {"started": True}


@app.get("/api/update/log", response_class=PlainTextResponse)
def update_log():
    return UPDATE_LOG.read_text(encoding="utf-8") if UPDATE_LOG.exists() else "(aucune mise a jour lancee)"


# ── flux live ───────────────────────────────────────────────────────────────

async def _broadcast(payload: dict):
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


async def _sampler():
    while True:
        try:
            snap = await asyncio.to_thread(_sample)
            history.append(snap)
            await _broadcast({"type": "sample", "data": snap})
        except Exception as e:
            await _broadcast({"type": "error", "data": str(e)})
        await asyncio.sleep(SAMPLE_S)


def _is_running(container: str) -> bool:
    try:
        r = _run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=15)
        return r.stdout.strip() == "true"
    except Exception:
        return False


async def _tail(service: str):
    """Suit les logs d'un service, en n'emettant que du nouveau.

    Deux pieges evites ici :
      - `docker logs -f` sur un conteneur ARRETE ne bloque pas : il recrache la fin du journal
        et rend la main. Se rattacher en boucle rejouait donc les memes lignes toutes les
        3 secondes, ce qui donnait l'illusion d'un service encore actif. On n'attache donc que
        si le conteneur tourne, et on attend sinon.
      - au re-attachement (apres un redemarrage), `--since` borne la lecture a ce qu'on n'a pas
        encore vu, au lieu de renvoyer un `--tail` deja diffuse.
    """
    container = f"{PROJECT}-{service}-1"
    since: Optional[str] = None
    while True:
        if not await asyncio.to_thread(_is_running, container):
            await asyncio.sleep(3)
            continue
        cmd = ["docker", "logs", "-f", container]
        cmd += (["--since", since] if since else ["--tail", "20"])
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            async for raw in proc.stdout:
                text = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip()
                if not text:
                    continue
                m = _LEVEL.search(text)
                now = time.time()
                entry = {"t": now, "svc": service,
                         "level": (m.group(1) if m else "INFO"), "msg": text[-400:]}
                # Reprise juste apres la derniere ligne vue, en RFC3339 comme docker l'attend.
                since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(now + 1)) + "Z"
                logs.append(entry)
                await _broadcast({"type": "log", "data": entry})
        except Exception:
            pass
        await asyncio.sleep(2)


@app.on_event("startup")
async def _startup():
    if not CONFIG_PATH.exists():
        _write_config({"version": 1,
                       "gcp_host": os.getenv("SIM_HOST", "192.168.100.11"),
                       "gcp_port": int(os.getenv("SIM_PORT", "8081")),
                       "window_s": 30, "downsample_ms": 100,
                       "needed_channels": _default_channels()})
    asyncio.create_task(_sampler())
    for svc in PIPELINE:
        asyncio.create_task(_tail(svc))


@app.websocket("/ws")
async def ws(sock: WebSocket):
    await sock.accept()
    clients.add(sock)
    try:
        await sock.send_json({"type": "hello", "data": {
            "boat": BOAT, "sample_s": SAMPLE_S, "history": list(history), "logs": list(logs),
            "config": get_config(), "toolkit": toolkit_version(), "pipeline": PIPELINE}})
        while True:
            await sock.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        clients.discard(sock)


@app.get("/", response_class=HTMLResponse)
def index():
    return (APP_DIR / "index.html").read_text(encoding="utf-8")
