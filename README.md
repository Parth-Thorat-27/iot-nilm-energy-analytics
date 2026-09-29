# IoT NILM & Energy Analytics for Shared Academic Spaces

A single ACS712 current sensor on an ESP32 watches the total current of a hostel room or lab.
A NILM (non-intrusive load monitoring) engine splits that one signal into per-appliance activity,
and a rules engine flags wasted energy (night-time use, standby/vampire load, appliances left on).

```
 Loads ─► ACS712 ─► 1k/2k divider + 100nF ─► ESP32 (GPIO34)       firmware/
                                              • 2 kHz sampling, true RMS over 10-cycle windows
                                              • apparent power, inrush peak
                                              • LCD · servo dial · alert LED
                                              │  MQTT JSON  hostel/<room>/power  (1 Hz)
                                              ▼
                          Mosquitto :1883 ─► bridge.py ─► InfluxDB :8086       backend/
                          (validate, tag site/room/phase)     ▲   │
                                                              │   ▼
                             nilm.py  (step detection ─► RandomForest/catalog ─► FSM ─►
                                       energy allocation ─► wastage rules ─► alerts/webhook)
                             anomaly.py (IsolationForest on 1-min features)
                                                              │
                                                              ▼
                                                   Grafana :3000  "NILM Energy Analytics"
```

| Spec phase | Where |
|---|---|
| 1. Signal conditioning & edge acquisition | [docs/wiring.md](docs/wiring.md), [firmware/src/main.cpp](firmware/src/main.cpp), [firmware/include/config.h](firmware/include/config.h) |
| 2. Ingestion & telemetry | [backend/bridge.py](backend/bridge.py), [backend/rooms.json](backend/rooms.json) (metadata tags) |
| 3. Dataset generation & ML disaggregation | [backend/nilm.py](backend/nilm.py) `label` / `train` / `run`, [backend/appliances.json](backend/appliances.json) |
| 4. Analytics, anomaly rules & UI | `nilm.py` rules, [backend/anomaly.py](backend/anomaly.py), [grafana/dashboards/energy.json](grafana/dashboards/energy.json) |
| Testing without hardware | [backend/simulate.py](backend/simulate.py) (simulated room + ground truth) |

## Running it

Everything runs in Docker, so there's no need to keep terminals open. Start Docker Desktop, then:

```powershell
docker compose up -d --build
```

Or in VS Code: **Terminal → Run Task → Start project**.

| Container | Job |
|---|---|
| `iota-mosquitto` | MQTT broker, port 1883 |
| `iota-influxdb` | time-series database, http://localhost:8086 |
| `iota-grafana` | dashboard, http://localhost:3000 |
| `iota-bridge` | stores every reading from every room |
| `iota-nilm` | disaggregation + wastage rules, every 2 min |
| `iota-anomaly` | IsolationForest anomaly scores, every 5 min |

All of them restart automatically, including after a reboot, as long as Docker Desktop is set to start with
Windows (Docker Desktop → Settings → General → *Start Docker Desktop when you sign in*).
**Data is only collected while the PC is on and Docker is running**, because the ESP32 doesn't buffer readings.

Open Grafana (user `admin`, password = `GRAFANA_PASSWORD` in `.env`), go to
**Dashboards → IoTA → NILM Energy Analytics**, and pick a room.

- **Logs:** `docker compose logs -f bridge nilm`, or the **Show live logs** task
- **Code changes:** `backend/` is mounted into the containers, so edits to the Python files, `rooms.json` or
  `appliances.json` apply after `docker compose restart bridge nilm anomaly`. Rebuild with `--build` only
  when `requirements.txt` changes.
- **Demo without hardware:** `docker compose --profile demo up -d simulator` publishes a live simulated room
  (`hostel/sim-room`). Stop it with `docker compose --profile demo stop simulator`. The database already
  holds 4 simulated days.

`.venv` is still used for one-off commands: `nilm.py label`/`train`, `export.py`, and `simulate.py --backfill-hours`.

## Collecting and exporting data

1. **Continuous log:** a flashed node plus the running stack stores one reading per second per room, with
   no other steps needed. Check it in Grafana, or look for `hostel/<room>/power` lines in `docker compose logs bridge`.
2. **Labelled signatures** for training: see [NILM workflow](#nilm-workflow) below.
3. **Export** for your report or your own analysis:

   ```powershell
   cd backend
   ..\.venv\Scripts\python.exe export.py --room room101                          # last 24 h -> Excel
   ..\.venv\Scripts\python.exe export.py --room room101 --from 2026-10-01 --to 2026-10-07 --resample 1min
   ..\.venv\Scripts\python.exe export.py --room room101 --hours 72 --format csv  # folder of CSVs
   ```

   Files go to `exports/`. The Excel file has one sheet each for readings, appliance_power, events, alerts,
   anomalies, summary (kWh and share per appliance) and signatures. Times are local. Excel is limited to
   about 12 days of raw 1 Hz readings, so use `--resample` or `--format csv` for longer ranges.

## Edge node (ESP32)

1. Wire it up following [docs/wiring.md](docs/wiring.md). **Read the mains safety note first.**
2. Edit [firmware/include/secrets.h](firmware/include/secrets.h): set `WIFI_SSID`, `WIFI_PASSWORD` and `ROOM_ID`.
   `MQTT_HOST` is this PC's IP (`192.168.12.76`).
3. Check [firmware/include/config.h](firmware/include/config.h): `ACS712_MV_PER_AMP` must match your
   module (20 A = 100, 30 A = 66, 5 A = 185). The same file sets the LCD address and alert threshold.
4. Open the `firmware` folder in VS Code, then click PlatformIO **Upload**, then **Monitor**
   (with mains unplugged while flashing).

Payload on `hostel/<room>/power`, once per second:

```json
{"device":"esp32-room101","room":"room101","current_a":1.234,"apparent_va":283.8,"power_w":283.8,
 "peak_a":1.95,"energy_wh":12.4,"voltage_v":230,"samples":2000,"rssi":-61,"uptime_s":3600}
```

`rssi` and `uptime_s` are sent every 10 s. `hostel/<room>/status` is a retained `online`/`offline` (MQTT last-will).

There's no voltage sensor, so power is estimated as `S = 230 V × Irms` (VA). This is exact for
resistive loads (heaters, soldering irons, kettles) and reads high for fans and SMPS chargers.

**Standalone alternative:** [arduino/overcurrent_cutoff](arduino/overcurrent_cutoff/overcurrent_cutoff.ino)
is an Arduino IDE sketch with no Wi-Fi, where the servo acts as a latching over-current trip arm (press BOOT
to reset). In the NILM firmware, the servo is a power dial instead.

## NILM workflow

**1. Out of the box**, step events are classified against the signature catalog in
[appliances.json](backend/appliances.json): step size, inrush ratio and rise time for a soldering iron, fan,
laptop charger, PC, heater and kettle. Edit it to match your room's appliances.

**2. Signature profiling (recommended).** For each appliance, with nothing else changing in the room,
switch it ON, wait about 15 s, switch it OFF, and repeat 3–5 times. Then run:

```powershell
python nilm.py label --room room101 --appliance soldering_iron --minutes 5
```

This appends the detected switch-on signatures to `backend/data/signatures.csv`. Once you've done this for
at least two appliances, train the classifier:

```powershell
python nilm.py train          # RandomForest, prints cross-validated accuracy, saves data/nilm_model.joblib
```

`nilm.py run` reloads the model on every pass. When the model is less than 50% sure, it falls back to the catalog.

**3. How events become appliance timelines.** Each switch-on opens an interval for its predicted appliance.
Each switch-off closes the running appliance whose power best matches |ΔP| (a finite-state machine). If a
switch-off has no match, that appliance was already running when the analysis window started, so it's
back-filled. Energy is then allocated per minute:

```
E_total = Σ E_appliance + E_baseline (always-on standby) + E_unclassified
```

### Wastage rules

These are configured per room in [rooms.json](backend/rooms.json), with times in `TZ_NAME` (Asia/Kolkata):

| Rule | Fires when | Default |
|---|---|---|
| `night_active_draw` | load > `night_active_w` for ≥ `night_active_min` inside the `night` window | 150 W, 5 min, 23:00–06:00 |
| `phantom_load` | the consumption floor never drops below `vampire_w` during an `unoccupied` window | 25 W |
| `appliance_runaway` | an appliance runs continuously longer than its `max_runtime_h` | per appliance in appliances.json |

Alerts go to the `alert` measurement and the dashboard. To push them to your phone or a chat, set
`ALERT_WEBHOOK_URL` in `.env`: an `https://ntfy.sh/<topic>` URL, or a Slack or Discord webhook. Only new
alerts that are active or recent (within 2 h) are sent, and each one is sent once.

Metrics in `nilm_summary` (last 24 h): total, standby (vampire) and unoccupied energy, unclassified
energy, and the **efficiency factor**, which is the share of energy that went to switched loads rather
than standby.

### Results on the simulator

With 4 simulated days, `python nilm.py train --from-truth --room sim-room --hours 96` gives 44/44
switch-ons detected with 0 false detections, and 97.7% cross-validated classification accuracy. With
catalog-only classification, 22/22 events on the latest day were labelled correctly, and all three rules
fired where the routine intends them to. The simulator is idealised: clean steps and one appliance
switching at a time. Real rooms have overlapping switching, appliances with varying loads and ADC noise,
so profile your own appliances and expect lower accuracy. The detector settings are at the top of `nilm.py`
(`STEP_MIN_W`, `MIN_STEADY_S`, and others).

> **Before going live on real hardware**, delete `backend/data/nilm_model.joblib`. It's currently trained on
> the simulator's signatures. Then profile your real appliances and run `nilm.py train` again.

## InfluxDB schema

| Measurement | Tags | Fields | Written by |
|---|---|---|---|
| `power` | site, room, phase, device | current_a, apparent_va, power_w, peak_a, energy_wh, voltage_v, samples, rssi, uptime_s | bridge |
| `node_status` | site, room | online | bridge |
| `appliance_power` | room, appliance (incl. `baseline`, `unclassified`) | power_w (1-min mean) | nilm |
| `nilm_event` | room, appliance, action, method | delta_w, rise_s, inrush_ratio, peak_a, confidence, before_w, after_w | nilm |
| `appliance_state` | room, appliance | on, power_w, since_min | nilm |
| `alert` | room, rule, appliance | message, value, duration_min, active | nilm |
| `nilm_summary` | room | total_wh, standby_wh, unoccupied_wh, unclassified_wh, efficiency_pct, events | nilm |
| `anomaly` | room | score (>0 = anomalous), is_anomaly, power_w | anomaly |
| `truth_event` | room, appliance, action | power_w | simulator only |

`nilm.py` deletes and rewrites its measurements for the analysis window on every pass, so rerunning is
safe and results improve as soon as the model does. Anything older than the window is kept as history
(raw data retention is 90 days, set by `INFLUXDB_RETENTION`).

## Housekeeping

```powershell
docker compose logs -f mosquitto              # watch nodes connect
docker compose --profile demo stop            # stop everything (data kept)
docker compose down -v                        # stop and wipe all data
docker compose exec anomaly python anomaly.py --once --score-minutes 1440   # rescore the past day
```

To remove the simulator data once real nodes are running (run from Git Bash; PowerShell 5.1 strips the inner quotes):

```bash
TOKEN=$(grep ^INFLUXDB_TOKEN= .env | cut -d= -f2-)
docker exec iota-influxdb influx delete --bucket energy --org iota --token "$TOKEN" \
  --start 1970-01-01T00:00:00Z --stop 2100-01-01T00:00:00Z --predicate 'room="sim-room"'
```

**Windows Firewall:** allow Docker on *Private* networks, or the ESP32 can't reach port 1883.
