"""Simulated room for testing the pipeline without hardware.

Publishes the same JSON as the firmware to <site>/<room>/power, from a daily routine
of appliances with distinct signatures:

    baseline 30 W (router, set-top box, chargers on standby)  - always on
    soldering_iron   60 W resistive, instant        - afternoon session + one left on at night
    ceiling_fan      75 W inductive, slow start      - evening to early morning
    laptop_charger   45 W SMPS, inrush spike         - evening
    pc_workstation  ~200 W dynamic, big inrush       - morning + evening
    room_heater    1500 W                            - 01:00 (night) and 07:00
    kettle         1800 W, ~3 min                    - three times a day

Ground-truth ON/OFF events go to the `truth_event` measurement so NILM accuracy can
be measured (`nilm.py train --from-truth`).

    python simulate.py                        # live, every 1 s
    python simulate.py --backfill-hours 24    # write 24 h of history to InfluxDB first, then live
    python simulate.py --backfill-hours 24 --count 0 --no-live
"""

import argparse
import json
import math
import random
import time
from datetime import datetime, timedelta, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import settings

VOLTAGE = 230.0
BASELINE_W = 30.0

APPLIANCES = {
    #                 power W, noise W, ramp s, inrush x, slow wander W
    "soldering_iron": (60, 1.5, 0, 1.0, 0),
    "ceiling_fan": (75, 2.0, 8, 1.5, 0),
    "laptop_charger": (45, 2.0, 0, 2.5, 0),
    "pc_workstation": (200, 6.0, 4, 3.5, 25),
    "room_heater": (1500, 10.0, 0, 1.1, 0),
    "kettle": (1800, 10.0, 0, 1.0, 0),
}

# (appliance, local start HH:MM, duration minutes, jitter minutes)
ROUTINE = [
    ("room_heater", "01:00", 40, 5),       # night-time draw -> rule 1
    ("room_heater", "07:00", 30, 5),
    ("kettle", "08:15", 3, 5),
    ("pc_workstation", "10:00", 180, 15),
    ("soldering_iron", "15:00", 60, 15),
    ("kettle", "17:00", 3, 5),
    ("ceiling_fan", "19:00", 390, 15),     # until ~01:30
    ("pc_workstation", "20:00", 210, 15),
    ("laptop_charger", "20:15", 150, 15),
    ("soldering_iron", "21:30", 150, 10),  # left on for 2.5 h -> runaway rule
    ("kettle", "23:40", 3, 5),
]


def schedule(start: datetime, end: datetime) -> list[tuple[str, datetime, datetime]]:
    """Deterministic per-day routine (seeded by date) covering [start, end]."""
    out = []
    d = (start.astimezone(settings.TZ) - timedelta(days=1)).date()
    while d <= end.astimezone(settings.TZ).date():
        rng = random.Random(d.toordinal())
        for name, hhmm, minutes, jitter in ROUTINE:
            local = datetime(d.year, d.month, d.day, int(hhmm[:2]), int(hhmm[3:]), tzinfo=settings.TZ)
            on = local + timedelta(minutes=rng.uniform(-jitter, jitter))
            off = on + timedelta(minutes=max(1.0, minutes + rng.uniform(-jitter, jitter)))
            if off >= start and on <= end:
                out.append((name, on.astimezone(timezone.utc), off.astimezone(timezone.utc)))
        d += timedelta(days=1)
    return out


class Room:
    def __init__(self, intervals, seed=0):
        self.intervals = intervals
        self.rng = random.Random(seed)

    def sample(self, t: datetime, step_s: float) -> tuple[float, float]:
        """Returns (apparent power W, peak current A) for the sample at time t."""
        w = BASELINE_W + self.rng.gauss(0, 1.5)
        inrush_a = 0.0
        for name, on, off in self.intervals:
            if not (on <= t < off):
                continue
            power, noise, ramp, inrush, wander = APPLIANCES[name]
            since = (t - on).total_seconds()
            level = power + wander * math.sin(t.timestamp() / 600.0)
            if ramp:
                level *= min(1.0, (since + 1) / ramp)
            w += level + self.rng.gauss(0, noise)
            if since < step_s:  # switch-on happened during this sample
                inrush_a += (inrush - 1.0) * math.sqrt(2) * power / VOLTAGE
        if self.rng.random() < 0.0005:  # rare odd spike for the anomaly detector
            w += self.rng.uniform(400, 900)
        w = max(w, 0.0)
        peak = math.sqrt(2) * w / VOLTAGE * (1 + self.rng.gauss(0, 0.01)) + inrush_a
        return w, peak


def reading(device: str, room: str, w: float, peak: float, energy_wh: float, uptime: float, meta: bool) -> dict:
    current = w / VOLTAGE
    if current < 0.10:  # same noise-floor clamp as the firmware
        current, w, peak = 0.0, 0.0, 0.0
    r = {
        "device": device,
        "room": room,
        "current_a": round(current, 3),
        "apparent_va": round(w, 1),
        "power_w": round(w, 1),
        "peak_a": round(peak, 3),
        "energy_wh": round(energy_wh, 2),
        "voltage_v": VOLTAGE,
        "samples": 2000,
    }
    if meta:
        r["rssi"] = random.randint(-70, -50)
        r["uptime_s"] = int(uptime)
    return r


def truth_points(room: str, intervals, start: datetime, end: datetime) -> list[Point]:
    pts = []
    for name, on, off in intervals:
        for action, t in (("on", on), ("off", off)):
            if start <= t <= end:
                pts.append(Point("truth_event").tag("room", room).tag("appliance", name).tag("action", action)
                           .field("power_w", float(APPLIANCES[name][0])).time(t, WritePrecision.S))
    return pts


def backfill(client: InfluxDBClient, site: str, room: str, device: str, hours: float, step_s: float) -> datetime:
    end = datetime.now(timezone.utc).replace(microsecond=0)
    start = end - timedelta(hours=hours)
    intervals = schedule(start, end)
    sim = Room(intervals, seed=1)
    phase = settings.room_config(room)["phase"]
    points, energy_wh, t, i = [], 0.0, start, 0
    while t < end:
        w, peak = sim.sample(t, step_s)
        energy_wh += w * step_s / 3600
        r = reading(device, room, w, peak, energy_wh, (t - start).total_seconds(), meta=i % 10 == 0)
        p = Point(settings.MEASUREMENT).tag("site", site).tag("room", room).tag("phase", phase).tag("device", device)
        for k, v in r.items():
            if k not in ("device", "room"):
                p.field(k, float(v) if isinstance(v, float) else v)
        points.append(p.time(t, WritePrecision.S))
        t += timedelta(seconds=step_s)
        i += 1
    points += truth_points(room, intervals, start, end)

    write = client.write_api(write_options=SYNCHRONOUS)
    for k in range(0, len(points), 10000):
        write.write(bucket=settings.INFLUXDB_BUCKET, record=points[k:k + 10000])
    print(f"Backfilled {i} readings + {len(points) - i} truth events ({hours} h) for {site}/{room}")
    return end


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--site", default="hostel")
    parser.add_argument("--room", default="sim-room")
    parser.add_argument("--interval", type=float, default=1.0, help="seconds between live readings")
    parser.add_argument("--backfill-hours", type=float, default=0, help="write this much history to InfluxDB first")
    parser.add_argument("--backfill-step", type=float, default=2.0, help="seconds between backfilled readings")
    parser.add_argument("--count", type=int, default=0, help="stop after N live readings (0 = forever)")
    parser.add_argument("--no-live", action="store_true", help="exit after the backfill")
    args = parser.parse_args()
    device = "simulator"

    influx = InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG)
    if args.backfill_hours > 0:
        backfill(influx, args.site, args.room, device, args.backfill_hours, args.backfill_step)
    if args.no_live:
        influx.close()
        return

    topic = f"{args.site}/{args.room}"
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"{device}-live")
    if settings.MQTT_USER:
        client.username_pw_set(settings.MQTT_USER, settings.MQTT_PASSWORD)
    client.will_set(f"{topic}/status", "offline", qos=1, retain=True)
    client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
    client.loop_start()
    client.publish(f"{topic}/status", "online", qos=1, retain=True)

    started = datetime.now(timezone.utc)
    intervals = schedule(started, started + timedelta(days=30))
    sim = Room(intervals, seed=2)
    truth_write = influx.write_api(write_options=SYNCHRONOUS)
    energy_wh, sent, prev = 0.0, 0, started
    try:
        while args.count == 0 or sent < args.count:
            now = datetime.now(timezone.utc)
            w, peak = sim.sample(now, args.interval)
            energy_wh += w * args.interval / 3600
            payload = reading(device, args.room, w, peak, energy_wh, (now - started).total_seconds(), meta=sent % 10 == 0)
            client.publish(f"{topic}/power", json.dumps(payload))
            truth = truth_points(args.room, intervals, prev, now)
            if truth:
                truth_write.write(bucket=settings.INFLUXDB_BUCKET, record=truth)
            print(json.dumps(payload))
            prev, sent = now, sent + 1
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.publish(f"{topic}/status", "offline", qos=1, retain=True).wait_for_publish(2)
        client.loop_stop()
        client.disconnect()
        influx.close()


if __name__ == "__main__":
    main()
