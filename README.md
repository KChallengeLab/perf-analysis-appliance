# perf-analysis-appliance

A headless, self-contained **Docker appliance** that runs the sailing performance pipeline **on the
boat**: it ingests the boat's live telemetry, detects points of interest (tacks, gybes, straight
lines…), analyses them, and ships the results ashore over Starlink — while staying fully autonomous
if the link drops.

Everything the boat sends is **outbound**. Nothing connects *into* the boat, so its dynamic
Starlink/CGNAT IP is irrelevant, and there are no inbound ports to expose.

```
 boat GCP DataViewer                    ┌───────────── the boat machine (Docker) ─────────────┐
 192.168.100.11:8081  ─────────────────►│  bridge ──► InfluxDB (local, the day's telemetry)   │
                                        │              │                                       │
                                        │   detection + inline analysis (one process)          │
                                        │              ├── POI + metrics ──► Neon   (outbound)  │
                                        │              └── manoeuvre ──► local queue            │
                                        │                                     │                 │
                                        │   replicator ── drains queue ──► ±window slice ──────┼──► shore InfluxDB  (outbound)
                                        │                                                       │
                                        │   control ── web UI :4267 ── edits the config JSON    │
                                        └───────────────────────────────────────────────────────┘
```

## The control panel — `http://<boat>:4267`

One page to run the appliance without touching files or compose. Live:

- **Telemetry source** — the GCP host/IP + port to read (real boat `192.168.100.11:8081`, sim
  `192.168.20.13:8085`). Changing it restarts the bridge automatically.
- **Egress parameters** — the POI window (± seconds), the sample rate (Hz), and the `needed`
  channel list. These hot-reload into the replicator **with no restart**.
- **Status** — every container's state + the egress queue depth.
- **Update toolkit from GitHub** — `git pull` (or submodule update) then rebuild the worker
  containers, with a streamed log.
- **Restart** any service.

All of it just edits one file — `config/appliance.json` (a Docker volume) — which is the appliance's
**single source of truth**:

```jsonc
{
  "version": 7,                 // bumped on every save
  "gcp_host": "192.168.100.11", // telemetry source (control-editable)
  "gcp_port": 8081,
  "window_s": 30,               // ± window around each manoeuvre
  "downsample_ms": 200,         // 200 ms = 5 Hz egress grid (0 = native rate)
  "needed_channels": ["Boat.TWA", "Boat.TWS_kts", "..."]  // [] / null = all channels
}
```

## Services

| Service | Role |
|---|---|
| `influxdb` | Linux InfluxDB — the day's telemetry (measurement `gcp_telemetry`). |
| `bridge` | GCP DataViewer → local Influx (source from the config JSON). |
| `detection` | `detection_worker --inline-analysis`: detects **and** analyses in one process (no Neon read-back); POIs+analyses → Neon, manoeuvres → local egress queue. |
| `replicator` | drains the local queue → ships each manoeuvre's ±window slice to the **shore** Influx (needed channels, downsampled, grouped, gzipped, idempotent). |
| `shore-influxdb` | test-rig stand-in for the real shore Influx (drop it on the boat; set `SHORE_INFLUX_URL`). |
| `control` | the web panel above (`:4267`). |

## Quick start

```bash
git clone --recursive <appliance-repo-url> perf-analysis-appliance
cd perf-analysis-appliance
cp .env.example .env          # fill INFLUX/SHORE tokens, Neon URL, HOST_PROJECT_DIR
docker compose up -d --build
```
Then open `http://localhost:4267` and set the source + egress parameters.

Prerequisites: **Docker with a Linux engine** (native Linux, or Docker Desktop / Docker-in-WSL2 on
Windows — InfluxDB-on-Windows is not supported). The boat machine must reach the GCP host on the LAN
and Neon + the shore Influx over the link (outbound only).

See **[`DEPLOYMENT.md`](DEPLOYMENT.md)** for the full boat runbook (prereqs, persistence, boat prep).

## Repository layout & keeping it up to date

The toolkit is upstream (`KChallengeLab/…`); the appliance pins it as a **git submodule** so a boat
build is reproducible and can still track upstream:

```
perf-analysis-appliance/          # this repo
├── docker-compose.yml            # the 6-service stack
├── control/                      # the :4267 web panel (own image)
├── .env / .env.example
├── DEPLOYMENT.md
└── sailing-data-toolkit/         # git submodule → the pipeline (bridge, workers, boats, models)
```

- **Pin a tested version:** the submodule records the exact toolkit commit the appliance was built
  and validated with.
- **Update:** the control panel's *Update* button (or `git submodule update --remote && docker
  compose up -d --build`) pulls the latest toolkit and rebuilds the workers.
- **Why not a monorepo:** the toolkit lives upstream and we rebase on its `main`; vendoring it would
  make pulling those updates painful.

> See `DEPLOYMENT.md` §Repository for the one-time GitHub setup (push the toolkit branch, create the
> appliance repo, `git submodule add`).

## Design notes

- **Timestamps** are the GCP frame's `tUTC` (authoritative); the bridge stamps each line with the
  latest tUTC of its flush window.
- **Detection ↔ analysis** is in-process and in-memory — the boat never round-trips a POI through
  Neon to hand it between two local processes.
- **The replicator's work list is a local file**, never a network query — so a dropped link never
  stops it from knowing *what* to send; entries queue and ship on reconnect (idempotent on `poi_id`).
- **Egress is small:** needed-channels + downsample + grouped + gzip ≈ **1–2 MB/h** on a busy leg.
