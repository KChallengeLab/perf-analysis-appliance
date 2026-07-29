# perf-analysis-docker-pack

Headless Docker appliance that runs the perf pipeline on the boat (or against the simulator):
a local **InfluxDB** (Linux container), the **bridge**, and **detection with inline analysis**.
Results land in **Neon**. See [`../onboard-appliance.md`](../onboard-appliance.md) for the full spec.

```
sim/boat :8085 ──► bridge ──► InfluxDB (container) ──► detection (--inline-analysis) ──► Neon
                                  gcp_telemetry / B3-CIS-TEST        POIs + analyses (egress-only)
```

## What's in the box

| Service | Image | Role |
|---|---|---|
| `influxdb` | `influxdb:2.7` | Linux InfluxDB; the day's telemetry. Bucket `B3-CIS-TEST`, measurement `gcp_telemetry`. |
| `bridge` | `perf-analysis-pack` | DataViewer `:8085` (`Boat.*`) → local Influx. Dashboard on `:8002`. |
| `detection` | `perf-analysis-pack` | `detection_worker --inline-analysis`: detects POIs **and** analyses them in one process, writes to Neon. No separate analysis service, no Neon read-back. |

`perf-analysis-pack` is built once from `../sailing-data-toolkit/Dockerfile` (sdt CLI + bridge + workers).

## Prerequisites

- **Docker Desktop with the WSL2 backend** (Linux containers — InfluxDB-on-Windows is unstable).
- Network reachability to the telemetry source (`SIM_HOST:8085`) and a reachable Neon branch.
- A **boat row** for `ONBOARD_BOAT` must exist in the Neon DB (`B3` / `boat_class=AC75` is already seeded in the test branch).

## Run

```bash
cp .env.example .env        # then edit: INFLUX_TOKEN/PASSWORD, SIM_HOST, Neon URL
docker compose build        # first build is large (toolkit pulls pandas/scipy/sklearn/...)
docker compose up           # or: up -d
```

Check it:
- **Bridge dashboard:** http://localhost:8002
- **InfluxDB UI:** http://localhost:8087 (login with `INFLUX_USERNAME` / `INFLUX_PASSWORD`) — look for measurement `gcp_telemetry` in bucket `B3-CIS-TEST`.
- **POIs/analyses:** in the Neon branch (`pois`, `straight_line_pois`, `manoeuvre_pois`, `*_analyses` for boat `B3`).
- **Logs:** `docker compose logs -f bridge` / `detection`.

Stop / reset:
```bash
docker compose down            # keep data
docker compose down -v         # also wipe the Influx volume
```

## Notes & current limitations

- **The sim must be actively sailing** for the nav/wind channels (`Boat.TWA`, `Boat.TWS_kts`, `Boat.VMG_kts`, the `_n` variants) to stream. Idle → raw sensors only → detection finds no manoeuvres. (All 76 B3 channels exist in `AC75_ILS.Blocks`; they just don't emit values when parked.)
- The `bridge` is the **existing sim bridge** parameterised for B3. A dedicated onboard B3 bridge will replace it; `--no-filter` will become the **Channel Hub manifest** filter (Phase 2).
- On the **boat**, set `SIM_HOST=host.docker.internal` to reach the DataViewer on the Windows host.
- The toolkit changes this relies on (`--inline-analysis`, env-overridable B3 bucket, `ONBOARD_BOAT`) live on branch `feat/onboard-b3-influx-bucket-override` — build from that branch.

## Phasing (per the spec)

- **Phase 0 (this):** sim → bridge → local Influx → detection+inline-analysis → test Neon.
- **Phase 1:** add the `replicator` (POI ±30 s window → shore Influx).
- **Phase 2:** bridge consumes the Channel Hub manifest (drop `--no-filter`).
- **Phase 3:** repoint to `host.docker.internal:8085` on the boat; prod Neon + prod shore Influx.
