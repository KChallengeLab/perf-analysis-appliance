# Deployment runbook — YODA (Yacht Onboard Data Analysis) on the CIS (boat machine)

How to bring the appliance up on the onboard **CIS** Windows machine. This is **Phase 3** of
[`../onboard-appliance.md`](../onboard-appliance.md), grounded in what was validated on the dev box
(2026-06-29: full stack built + run; replicator copy/idempotency validated on real data).

```
boat GCP 192.168.100.11:8081 ──► bridge ──► InfluxDB (local container) ──► detection (--inline-analysis) ──► Neon
                                              gcp_telemetry / B3-CIS-TEST          POIs + analyses (outbound)
                                                     │
                                                     └──► replicator ──► shore InfluxDB (manoeuvre ±30s windows, outbound)
```

Everything the boat sends is **outbound** (POIs→Neon, windows→shore Influx). Nothing connects *into*
the boat → the boat's Starlink/CGNAT IP is irrelevant.

> **Day-to-day you drive it from the control panel** at `http://<boat>:4267` — telemetry source
> (GCP host/port), POI window, sample Hz, needed channels, status, restart, and "update from GitHub".
> It just edits `config/appliance.json`. The steps below are the one-time bring-up.

---

## Repository & updates (one-time GitHub setup)

The appliance pins the toolkit as a **git submodule** — reproducible boat builds, still able to track
upstream. Set it up once:

```bash
# 1. push the toolkit work branch to a remote you control (a fork or the org)
cd sailing-data-toolkit
git push <your-remote> feat/onboard-b3-influx-bucket-override

# 2. create an empty "perf-analysis-appliance" repo on GitHub, then in the pack:
cd ../perf-analysis-docker-pack
git submodule add -b feat/onboard-b3-influx-bucket-override <toolkit-remote-url> sailing-data-toolkit
git add . && git commit -m "Appliance + toolkit submodule" && git push
```

Deploy / clone anywhere:
```bash
git clone --recursive <appliance-repo-url>          # brings the toolkit submodule
# later, to update the toolkit to its latest:
git submodule update --remote && docker compose up -d --build   # (the control panel's Update button does this)
```

> **Not a monorepo:** the toolkit is upstream (`KChallengeLab/…`) and we rebase on its `main`;
> vendoring it into one repo would make those updates painful. Submodule keeps both clean.

---

## 0. Before the CIS arrives — prepare these (so you're ready)

| Prepare | Notes |
|---|---|
| **Pre-built image** (recommended) | On a dev box: `docker save perf-analysis-pack:latest \| gzip > perf-analysis-pack.tar.gz`. Put it + this pack on a USB. Avoids needing apt/pip on the boat. |
| **Toolkit branch committed** | `feat/onboard-b3-influx-bucket-override` must be reachable (git remote) or bundled, if you build on the CIS instead of loading the image. |
| **`.env` values decided** | Influx token/password (generate), **shore Influx URL (hostname) + token**, **target Neon URL**, `ONBOARD_BOAT=B3`. See §3. |
| **Shore InfluxDB reachable** | A real shore Influx (cloud or on-prem) with a **stable hostname** + a write token + a bucket. This is the replicator's destination. |
| **Target Neon ready** | Schema + a **B3 boat row** (`boat_class=AC75`). See §4. |
| **needed-channel list** | The set the replicator ships over Starlink (the 76 B3 channels). See §5. |
| **Persistence decision** | Docker Desktop (start-on-login) vs Engine-in-WSL + scheduled task. See §7. |
| **Confirm boat GCP endpoint** | Default is `192.168.100.11:8081` (verified read-only). Confirm the IP/port as seen *from the CIS* and whether the CIS is that same host (→ `host.docker.internal`). |

---

## 1. Prerequisites on the CIS (one-time)

### 1a. WSL2 + Ubuntu  (admin PowerShell)
```powershell
wsl --install        # installs WSL2 + Ubuntu, then REBOOT
```
On first Ubuntu launch, set a UNIX username + password.

### 1b. Docker  (inside Ubuntu — `wsl`)
```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"
sudo systemctl enable --now docker          # systemd is on by default on recent WSL
```
Then from **PowerShell**: `wsl --shutdown`, reopen. Verify: `docker version` (client+server).

> InfluxDB **must** be the Linux container (never Influx-on-Windows). WSL2 gives us that.

### 1c. Reachability checks (from inside Ubuntu)
```bash
python3 - <<'PY'
import socket
def t(h,p):
    s=socket.socket(); s.settimeout(4); r=s.connect_ex((h,p)); s.close()
    print(('OPEN ' if r==0 else 'CLOSED')+f' {h}:{p}')
t('192.168.100.11',8081)   # boat GCP  (LAN)
# also confirm the shore Influx host + the Neon host resolve & connect (outbound, over Starlink)
PY
```
If the LAN host is unreachable from WSL (it was reachable on the dev box), enable **mirrored
networking**: create `%UserProfile%\.wslconfig` with `[wsl2]\nnetworkingMode=mirrored`, then
`wsl --shutdown`.

---

## 2. Get the appliance onto the CIS

**Option A — pre-built image (recommended for the boat):**
```bash
# copy perf-analysis-pack.tar.gz + the perf-analysis-docker-pack/ folder onto the CIS, then:
gunzip -c perf-analysis-pack.tar.gz | docker load
```
Then comment out the `build:` lines (use the loaded `image:` only) — or leave them; compose uses the
image if present.

**Option B — build on the CIS** (needs internet for apt/pip): clone the toolkit branch + this pack
**side by side**, then `docker compose build`. First build is large (pandas/scipy/…); the Dockerfile
already installs `gcc libc6-dev libpq-dev` so psycopg2 compiles.

---

## 3. Configure `.env`  (in `perf-analysis-docker-pack/`)
```bash
cp .env.example .env
```
Set for the boat:
```ini
# Telemetry source = the real boat Gomboc GCP
SIM_HOST=192.168.100.11        # or host.docker.internal if the CIS IS the GCP host
SIM_PORT=8081                  # ⚠️ 8081 (real boat) — NOT 8085 (that was the sim)

ONBOARD_BOAT=B3

# Local InfluxDB container (generate a strong token/password)
INFLUX_TOKEN=<openssl rand -hex 32>
INFLUX_PASSWORD=<strong>

# Shore InfluxDB — the replicator's destination. Use a HOSTNAME (IP-change transparent).
SHORE_INFLUX_URL=https://shore-influx.<your-domain>
SHORE_INFLUX_TOKEN=<shore write token>
SHORE_INFLUX_ORG=metrics-org
SHORE_INFLUX_BUCKET=B3            # prod shore bucket (B3-CIS-TEST while staging)

# Neon (POIs + analyses) — must end in _POSTGRES_URL
AC75_ONBOARD_TEST_POSTGRES_URL=postgresql://…neon.tech/…?sslmode=require&channel_binding=require
```
**Remove the `shore-influxdb` service** from `docker-compose.yml` for the boat — that container was the
local test destination; on the boat `SHORE_INFLUX_URL` points at the real shore Influx.

---

## 4. Prepare Neon (target)

The target Neon needs the **schema** and a **B3 boat row** (workers resolve the class from the DB).
If starting from a clone of prod the schema is there; ensure a row exists:
```sql
-- boat_class is the 'boatclass' enum; AC75 is a valid value
INSERT INTO boats (boat_class, name, created_date, created_by, updated_date, updated_by, version)
VALUES ('AC75'::boatclass, 'B3', now(), 'onboard-setup', now(), 'onboard-setup', 1)
ON CONFLICT (name) DO NOTHING;
```
(Schema bootstrap, if a fresh DB: `sailing-data-toolkit/examples/01_setup_SQL_database.py`.)

---

## 5. Channel scope (the `needed` set) — important on the real boat

The real boat exposes **~7 710 channels**. The B3 profile needs **76** of them (all present in
`AC75_ILS.Blocks`).

- **Local Influx (bridge → local):** streaming broadly is *local only* (no network) and tolerable for
  the day, but to keep it lean prefer writing only the needed set. The current compose runs the bridge
  with `--no-filter --write-groups '*'` (fine for the sim; a firehose on the boat).
- **Over Starlink (replicator → shore):** this is where `needed` **must** be enforced. Pass the 76 B3
  channels to the replicator so only those windows leave the boat:
  ```yaml
  # replicator service command, add:
  - "--channels=Boat.TWA,Boat.TWS_kts,Boat.Speed_kts,Boat.Heel,Boat.CWA,...(the 76)"
  ```
  (Until the Channel Hub manifest exists, keep this list in the `.env`/compose. The full 76 are in
  `sailing-data-toolkit/.../boats/b3.py::get_channel_groups()`.)

> The Channel-Hub-driven bridge filter + manifest is the eventual answer (spec Phase 2); for first boat
> deploy, the replicator `--channels` list is the lever that protects Starlink.

---

## 6. Run
```bash
cd perf-analysis-docker-pack
docker compose up -d           # influx becomes healthy, then bridge/detection/replicator start
docker compose ps
```

---

## 7. Headless persistence (survive reboot, no logged-in session)

WSL terminates the distro when no session is attached (we hit this — containers got SIGTERM’d). Pick one:

- **Docker Desktop, "Start Docker Desktop when you log in"** (+ WSL2 backend). Keeps the distro + daemon
  alive; `restart: unless-stopped` brings containers back after a reboot. ⭐ simplest for the boat.
- **Engine-in-WSL + Task Scheduler**: a task at boot/logon that runs
  `wsl -d Ubuntu -- bash -lc "cd /path && docker compose up -d"` and keeps a session
  (e.g. a small `wsl -d Ubuntu -- sleep infinity` keep-alive task). systemd auto-starts dockerd; the
  scheduled task holds the distro up.

Either way, `restart: unless-stopped` is already set on every service.

---

## 8. Verify (boat must be **sailing** for POIs)
```bash
docker compose logs -f bridge        # "Connecting to ws://192.168.100.11:8081" then "InfluxDB write [merged]: N pts"
docker compose logs -f detection     # POIs detected → "Inline analysis ENABLED", analyses written
docker compose logs -f replicator    # "Shipped poi <id> (TACK) @ <t>: N points → shore"
```
- **Local Influx UI:** `http://localhost:8087` — measurement `gcp_telemetry`, bucket `B3-CIS-TEST`.
- **POIs/analyses:** in Neon (`manoeuvre_pois`, `straight_line_pois`, `*_analyses` for boat `B3`).
- **Shore Influx:** the manoeuvre windows tagged `boat=B3`, `poi_id=<id>`.

> ⚠️ **Detection needs the boat actually sailing.** It hard-requires `Boat.TWS_kts` (true wind speed):
> if the boat streams only position/attitude (TWA, Speed) but not the computed wind solution, detection
> raises `Missing required channels: ['TWS_kts']` every cycle and produces no POIs. Confirm the boat is
> in a real sailing run (wind solution converged) — that's a boat-state requirement, not a bug.

---

## 9. Day-2 ops
```bash
docker compose logs -f <service>          # tail
docker compose restart <service>          # restart one service
docker compose down                       # stop (keep data)
docker compose down -v                    # stop + wipe Influx volumes (fresh start)
docker compose pull && docker compose up -d --build   # update after a code change
```
- **Purge the day’s local Influx** on demand (spec: local keeps the current day): delete the bucket’s
  data or `down -v`. Full-resolution archive leaves nightly by disk (`.db`, existing pipelines).
- **Replicator cursor / spool:** in the `spool` volume (`replicator_cursor_B3.json`). Deleting it
  re-ships from scratch (idempotent on `poi_id`, so safe).

---

## 10. Troubleshooting (gotchas already hit)

| Symptom | Cause / fix |
|---|---|
| Containers cycle / `Received signal 15` every ~20s | WSL killed the distro (no attached session). → §7 persistence (Docker Desktop or a keep-alive task). |
| `bridge … Errno 113 … 192.168.20.13/192.168.100.11:8085/8081` | Source unreachable or off. Check the boat GCP is up and `SIM_HOST/SIM_PORT` correct (**8081** on the real boat). LAN unreachable from WSL → mirrored networking (§1c). |
| `detection … Missing required channels: ['TWS_kts']` | Boat not in a full sailing run (wind solution not streaming). §8. |
| build fails on `psycopg2` (`stdlib.h` / `pg_config`) | needs `gcc libc6-dev libpq-dev` — already in the Dockerfile; rebuild. |
| `docker: command not found` after install | `wsl --shutdown` + reopen so the `docker` group applies (or use `sudo docker`). |
| replicator `No new manoeuvres past id 0` forever | No manoeuvre POIs in Neon yet → upstream (detection not producing). |

---

## State at hand-off (2026-06-29)

**Built + validated on the dev box:** image builds; influx/bridge/detection(+inline-analysis)/replicator/
shore all run; replicator fetch+ship+**idempotency** validated on real telemetry; real boat verified
read-only at `192.168.100.11:8081` (76/76 B3 channels present).

**Still to finalize for the boat:** commit/ship the toolkit branch, real **shore Influx** endpoint,
**prod Neon** decision + B3 seeded there, the replicator **`--channels`** needed-list, and the
**persistence** mechanism (§7).
