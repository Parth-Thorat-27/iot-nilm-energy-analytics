"""NILM engine: disaggregate one aggregate current signal into appliance activity.

Pipeline (per room, over the last --hours of data):
  1. Clean      median filter (removes single-sample ADC spikes), <0.1 A already clamped at the edge.
  2. Detect     steady-state segmentation: a step event is a move of >= STEP_MIN_W between two
                levels that each hold for >= MIN_STEADY_S. Records dP, rise time, inrush ratio.
  3. Classify   ON events -> appliance, with the trained RandomForest (data/nilm_model.joblib)
                when confident, else nearest match in appliances.json.
  4. Track      finite-state machine: each ON opens an interval; each OFF closes the running
                appliance whose power best matches |dP|. An OFF with no match means the
                appliance was already on when the window started, so it is back-filled.
  5. Allocate   1-minute power per appliance, always-on baseline (rolling min of the residual)
                and unclassified remainder:  E_total = sum(E_appliance) + E_baseline + E_unclassified
  6. Rules      night-time active draw, phantom/vampire baseline, appliance runaway -> alerts
                (+ optional webhook). Metrics: standby/unoccupied energy, efficiency factor.

Everything is written back to InfluxDB (appliance_power, nilm_event, appliance_state, alert,
nilm_summary). The window is deleted and rewritten each pass, so results are idempotent.

    python nilm.py run                    # loop every 2 min over the last 24 h
    python nilm.py run --once
    python nilm.py label --room room101 --appliance soldering_iron --minutes 10
    python nilm.py train                  # RandomForest from data/signatures.csv
    python nilm.py train --from-truth --room sim-room --hours 24   # labels from the simulator
"""

import argparse
import json
import logging
import math
import time
import urllib.request
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import joblib
import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.warnings import MissingPivotFunction
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold, cross_val_score

import settings

log = logging.getLogger("nilm")
warnings.simplefilter("ignore", MissingPivotFunction)  # truth query is intentionally long-format

# ---- Detection tunables -------------------------------------------------------
STEP_MIN_W = 30.0       # smallest step treated as an appliance switching
MIN_STEADY_S = 6.0      # a level must hold this long to count as steady
MAX_SETTLE_S = 25.0     # how long a transition may take to settle
GAP_S = 30.0            # a data gap longer than this resets detection
MEDIAN_WINDOW = 3
OFF_MATCH_TOL = 0.4     # |dP_off| must be within 40% of the running appliance's power
MODEL_MIN_PROBA = 0.5
BASELINE_WINDOW_MIN = 30
NOTIFY_MAX_AGE = timedelta(hours=2)
DEFAULT_VOLTAGE = 230.0
SQRT2 = math.sqrt(2)

MODEL_FEATURES = ["abs_delta_w", "log_delta_w", "inrush_ratio", "rise_s"]
SIGNATURES_CSV = settings.DATA_DIR / "signatures.csv"
MODEL_PATH = settings.DATA_DIR / "nilm_model.joblib"
ALERT_STATE = settings.DATA_DIR / "alert_state.json"


@dataclass
class Event:
    time: pd.Timestamp
    delta_w: float
    before_w: float
    after_w: float
    rise_s: float
    peak_a: float
    inrush_ratio: float
    appliance: str = "unknown"
    method: str = ""
    confidence: float = 0.0

    @property
    def action(self) -> str:
        return "on" if self.delta_w > 0 else "off"

    def features(self) -> dict:
        a = abs(self.delta_w)
        return {"abs_delta_w": a, "log_delta_w": math.log(a), "inrush_ratio": self.inrush_ratio, "rise_s": self.rise_s}


@dataclass
class Interval:
    appliance: str
    start: pd.Timestamp
    end: pd.Timestamp | None
    power_w: float
    inferred: bool = False  # already on at window start (seen only its OFF)


# ================================================================== data access

def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _frame(result) -> pd.DataFrame:
    if isinstance(result, list):
        result = pd.concat(result, ignore_index=True) if result else pd.DataFrame()
    return result


def list_rooms(client: InfluxDBClient, hours: float) -> list[str]:
    q = f'''import "influxdata/influxdb/schema"
schema.tagValues(bucket: "{settings.INFLUXDB_BUCKET}", tag: "room",
  predicate: (r) => r._measurement == "{settings.MEASUREMENT}", start: -{int(hours * 60)}m)'''
    return [rec.get_value() for table in client.query_api().query(q) for rec in table.records]


def fetch_power(client: InfluxDBClient, room: str, start: datetime, stop: datetime) -> pd.DataFrame:
    q = f'''from(bucket: "{settings.INFLUXDB_BUCKET}")
  |> range(start: {_iso(start)}, stop: {_iso(stop)})
  |> filter(fn: (r) => r._measurement == "{settings.MEASUREMENT}" and r.room == "{room}")
  |> filter(fn: (r) => r._field == "apparent_va" or r._field == "peak_a" or r._field == "current_a" or r._field == "voltage_v")
  |> group()
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> sort(columns: ["_time"])'''
    df = _frame(client.query_api().query_data_frame(q))
    if df.empty:
        return df
    df = df.set_index("_time").sort_index()
    df = df[~df.index.duplicated()]
    voltage = df["voltage_v"].median() if "voltage_v" in df else DEFAULT_VOLTAGE
    if "apparent_va" not in df:
        df["apparent_va"] = df["current_a"] * voltage
    if "peak_a" not in df:
        df["peak_a"] = df["current_a"] * SQRT2
    df["peak_a"] = df["peak_a"].fillna(df["current_a"] * SQRT2)
    df.attrs["voltage"] = float(voltage) if not math.isnan(voltage) else DEFAULT_VOLTAGE
    return df[["apparent_va", "peak_a"]].dropna(subset=["apparent_va"])


def fetch_truth(client: InfluxDBClient, room: str, start: datetime, stop: datetime) -> pd.DataFrame:
    q = f'''from(bucket: "{settings.INFLUXDB_BUCKET}")
  |> range(start: {_iso(start)}, stop: {_iso(stop)})
  |> filter(fn: (r) => r._measurement == "truth_event" and r.room == "{room}" and r._field == "power_w")
  |> group()
  |> keep(columns: ["_time", "_value", "appliance", "action"])
  |> sort(columns: ["_time"])'''
    return _frame(client.query_api().query_data_frame(q))


# ================================================================== detection

def clean(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["p"] = out["apparent_va"].rolling(MEDIAN_WINDOW, center=True, min_periods=1).median()
    return out


def detect_events(df: pd.DataFrame) -> list[Event]:
    """Steady-state segmentation over the cleaned series."""
    if len(df) < 5:
        return []
    ts = df.index.asi8 / 1e9
    p = df["p"].to_numpy(dtype=float)
    pk = df["peak_a"].to_numpy(dtype=float)
    volts = df.attrs.get("voltage", DEFAULT_VOLTAGE)
    n = len(p)
    dt = float(np.median(np.diff(ts)))
    w = max(3, int(round(MIN_STEADY_S / dt)) + 1)  # samples spanning MIN_STEADY_S

    def steady_from(k: int, until: float) -> int | None:
        m = k
        while m + w <= n and ts[m] <= until:
            seg = p[m:m + w]
            if ts[m + w - 1] - ts[m] > (w + 1) * dt:  # gap inside the window
                return None
            if seg.max() - seg.min() <= max(STEP_MIN_W, 0.05 * abs(float(np.median(seg)))):
                return m
            m += 1
        return None

    events: list[Event] = []
    level: float | None = None
    i = 0
    while i < n:
        if level is None:
            s = steady_from(i, math.inf)
            if s is None:
                # could be a gap inside the window: skip past it and try again
                i += 1
                continue
            level = float(np.median(p[s:s + w]))
            i = s + w
            continue
        if ts[i] - ts[i - 1] > GAP_S:
            level = None
            continue
        dev = p[i] - level
        if abs(dev) < STEP_MIN_W:
            level += 0.05 * dev  # follow slow drift
            i += 1
            continue

        start = i
        s = steady_from(start, ts[start] + MAX_SETTLE_S)
        if s is None:  # still moving: look again from the next sample
            i += 1
            continue
        new_level = float(np.median(p[s:s + w]))
        delta = new_level - level
        if abs(delta) >= STEP_MIN_W:
            peak = float(np.nanmax(pk[max(start - 1, 0):s + 1]))
            if delta > 0:
                steady_peak_step = SQRT2 * delta / volts
                inrush = (peak - SQRT2 * level / volts) / steady_peak_step
            else:
                inrush = 1.0
            events.append(Event(
                time=df.index[start], delta_w=delta, before_w=level, after_w=new_level,
                rise_s=float(ts[s] - ts[start]), peak_a=peak, inrush_ratio=float(np.clip(inrush, 0.5, 10)),
            ))
        level = new_level
        i = s + w
    return events


# ================================================================== classification

class Classifier:
    def __init__(self):
        self.model = None
        if MODEL_PATH.exists():
            bundle = joblib.load(MODEL_PATH)
            self.model = bundle["model"]
            log.info("Loaded NILM model (%s)", ", ".join(self.model.classes_))

    @staticmethod
    def catalog(ev: Event) -> tuple[str, float]:
        a = abs(ev.delta_w)
        best, best_score = "unknown", math.inf
        for name, sig in settings.APPLIANCES.items():
            rel = abs(a - sig["power_w"]) / sig["power_w"]
            if rel > sig["tolerance"]:
                continue
            score = rel
            if ev.delta_w > 0:  # transient shape only exists at switch-on
                score += 0.15 * abs(math.log(ev.inrush_ratio / sig["inrush_ratio"]))
                score += 0.05 * abs(ev.rise_s - sig["rise_s"])
            if score < best_score:
                best, best_score = name, score
        return best, (max(0.0, 1.0 - best_score) if best != "unknown" else 0.0)

    def classify(self, ev: Event) -> None:
        if self.model is not None and ev.delta_w > 0:
            proba = self.model.predict_proba(pd.DataFrame([ev.features()])[MODEL_FEATURES])[0]
            k = int(np.argmax(proba))
            if proba[k] >= MODEL_MIN_PROBA:
                ev.appliance, ev.confidence, ev.method = self.model.classes_[k], float(proba[k]), "model"
                return
        ev.appliance, ev.confidence = self.catalog(ev)
        ev.method = "catalog"


# ================================================================== state tracking

def track(events: list[Event], clf: Classifier, window_start: pd.Timestamp) -> list[Interval]:
    active: list[Interval] = []
    done: list[Interval] = []
    for ev in events:
        if ev.delta_w > 0:
            clf.classify(ev)
            active.append(Interval(ev.appliance, ev.time, None, ev.delta_w))
            continue
        a = -ev.delta_w
        best = min(active, key=lambda iv: abs(iv.power_w - a) / iv.power_w, default=None)
        if best is not None and abs(best.power_w - a) / best.power_w <= OFF_MATCH_TOL:
            best.end = ev.time
            active.remove(best)
            done.append(best)
            ev.appliance, ev.method = best.appliance, "fsm"
            ev.confidence = 1.0 - abs(best.power_w - a) / best.power_w
        else:
            clf.classify(ev)  # was running before the window (or its ON was missed)
            done.append(Interval(ev.appliance, window_start, ev.time, a, inferred=True))
    return done + active  # active ones have end=None (still on)


# ================================================================== allocation

def allocate(df: pd.DataFrame, intervals: list[Interval], now: pd.Timestamp) -> pd.DataFrame:
    """1-minute table: total, per-appliance, baseline, unclassified (all mean W over the minute)."""
    total = df["apparent_va"].resample("1min").mean()
    idx = total.index
    table = pd.DataFrame({"total": total})
    bin_s = 60.0
    starts = idx.asi8 / 1e9
    for iv in intervals:
        s = iv.start.timestamp()
        e = (iv.end or now).timestamp()
        overlap = np.clip(np.minimum(starts + bin_s, e) - np.maximum(starts, s), 0, bin_s)
        col = table.get(iv.appliance, pd.Series(0.0, index=idx))
        table[iv.appliance] = col + iv.power_w * overlap / bin_s
    apps = [c for c in table.columns if c != "total"]
    has_data = table["total"].notna()
    table.loc[~has_data, apps] = np.nan
    residual = (table["total"] - table[apps].sum(axis=1)).where(has_data)
    table["baseline"] = residual.rolling(BASELINE_WINDOW_MIN, min_periods=5).min().clip(lower=0)
    table["baseline"] = table["baseline"].fillna(residual.clip(lower=0))
    table["unclassified"] = (residual - table["baseline"]).clip(lower=0)
    return table


def in_windows(index: pd.DatetimeIndex, windows: list[list[str]]) -> np.ndarray:
    local = index.tz_convert(settings.TZ)
    minutes = local.hour * 60 + local.minute
    mask = np.zeros(len(index), dtype=bool)
    for a, b in windows:
        am = int(a[:2]) * 60 + int(a[3:])
        bm = int(b[:2]) * 60 + int(b[3:])
        mask |= (minutes >= am) & (minutes < bm) if am < bm else (minutes >= am) | (minutes < bm)
    return mask


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index pairs of consecutive True values."""
    runs, start = [], None
    for i, v in enumerate(mask):
        if v and start is None:
            start = i
        elif not v and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(mask)))
    return runs


# ================================================================== rules + metrics

@dataclass
class Alert:
    rule: str
    start: pd.Timestamp
    minutes: float
    value: float
    active: bool
    message: str
    appliance: str = "-"


def evaluate_rules(room: str, table: pd.DataFrame, intervals: list[Interval], now: pd.Timestamp) -> list[Alert]:
    cfg = settings.room_config(room)
    alerts: list[Alert] = []
    total = table["total"]
    last_bin = len(table) - 1

    # Rule 1: night-time / unoccupied active draw
    night = in_windows(table.index, [cfg["night"]])
    over = night & (total > cfg["night_active_w"]).to_numpy()
    for a, b in _runs(over):
        if b - a < cfg["night_active_min"]:
            continue
        seg = total.iloc[a:b]
        start_local = table.index[a].tz_convert(settings.TZ)
        alerts.append(Alert(
            "night_active_draw", table.index[a], b - a, float(seg.max()), b - 1 >= last_bin - 1,
            f"{room}: {seg.mean():.0f} W drawn at night from {start_local:%H:%M} for {b - a} min "
            f"(peak {seg.max():.0f} W, {seg.sum() / 60:.0f} Wh) - unattended appliance?",
        ))

    # Rule 2: phantom / vampire load - the room's consumption floor while unoccupied stays high.
    # Uses measured total (2nd percentile of 1-min means), not the reconstruction, so NILM errors can't mask it.
    unocc = in_windows(table.index, cfg["unoccupied"])
    for a, b in _runs(unocc):
        base = total.iloc[a:b].dropna()
        if len(base) < 60:
            continue
        floor = float(base.quantile(0.02))
        if floor > cfg["vampire_w"]:
            start_local = table.index[a].tz_convert(settings.TZ)
            alerts.append(Alert(
                "phantom_load", table.index[a], b - a, floor, b - 1 >= last_bin - 1,
                f"{room}: standby load never dropped below {floor:.0f} W while unoccupied from "
                f"{start_local:%H:%M} (limit {cfg['vampire_w']} W) - idle equipment left in standby",
            ))

    # Rule 3: appliance runaway (continuous runtime beyond its normal bound)
    for iv in intervals:
        limit_h = settings.APPLIANCES.get(iv.appliance, {}).get("max_runtime_h")
        if not limit_h:
            continue
        runtime_h = ((iv.end or now) - iv.start).total_seconds() / 3600
        if runtime_h > limit_h:
            alerts.append(Alert(
                "appliance_runaway", iv.start, runtime_h * 60, runtime_h, iv.end is None,
                f"{room}: {iv.appliance} ran {runtime_h:.1f} h continuously (normal max {limit_h} h)"
                + (" and is STILL ON" if iv.end is None else ""),
                appliance=iv.appliance,
            ))
    return alerts


def summarize(room: str, table: pd.DataFrame) -> dict:
    cfg = settings.room_config(room)
    wh = lambda s: float(np.nansum(s.to_numpy()) / 60.0)  # noqa: E731 - mean W per minute -> Wh
    apps = [c for c in table.columns if c not in ("total", "baseline", "unclassified")]
    unocc = in_windows(table.index, cfg["unoccupied"])
    total_wh = wh(table["total"])
    standby_wh = wh(table["baseline"])
    return {
        "total_wh": total_wh,
        "standby_wh": standby_wh,
        "unclassified_wh": wh(table["unclassified"]),
        "unoccupied_wh": wh(table["total"][unocc]),
        "efficiency_pct": 100.0 * (total_wh - standby_wh) / total_wh if total_wh > 0 else 0.0,
        "per_appliance_wh": {a: wh(table[a]) for a in apps},
    }


# ================================================================== output

def notify(alerts: list[Alert], room: str, now: pd.Timestamp) -> None:
    settings.DATA_DIR.mkdir(exist_ok=True)
    sent = set(json.loads(ALERT_STATE.read_text())) if ALERT_STATE.exists() else set()
    new = []
    for al in alerts:
        key = f"{room}|{al.rule}|{al.appliance}|{al.start.isoformat()}"
        recent = al.active or (now - (al.start + timedelta(minutes=al.minutes))) < NOTIFY_MAX_AGE
        if key in sent or not recent:
            continue
        log.warning("ALERT %s", al.message)
        if settings.ALERT_WEBHOOK_URL:
            try:
                if "ntfy" in settings.ALERT_WEBHOOK_URL:  # ntfy.sh takes a plain-text body
                    req = urllib.request.Request(settings.ALERT_WEBHOOK_URL, al.message.encode(), {"Title": f"Energy alert: {al.rule}"})
                else:  # Slack reads "text", Discord reads "content"
                    body = json.dumps({"text": al.message, "content": al.message}).encode()
                    req = urllib.request.Request(settings.ALERT_WEBHOOK_URL, body, {"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).close()
            except Exception as e:
                log.error("Webhook failed: %s", e)
                continue  # retry next pass
        new.append(key)
    if new:
        ALERT_STATE.write_text(json.dumps(sorted(sent | set(new)), indent=0))


def write_results(client, room, start, now, events, intervals, table, alerts, summary):
    bucket, org = settings.INFLUXDB_BUCKET, settings.INFLUXDB_ORG
    delete_api = client.delete_api()
    for m in ("appliance_power", "nilm_event", "alert"):
        delete_api.delete(start, now, f'_measurement="{m}" AND room="{room}"', bucket=bucket, org=org)

    pts: list[Point] = []
    for col in table.columns:
        if col == "total":
            continue
        for t, v in table[col].dropna().items():
            pts.append(Point("appliance_power").tag("room", room).tag("appliance", col)
                       .field("power_w", round(float(v), 2)).time(t, WritePrecision.S))
    for ev in events:
        pts.append(Point("nilm_event").tag("room", room).tag("appliance", ev.appliance)
                   .tag("action", ev.action).tag("method", ev.method)
                   .field("delta_w", round(float(ev.delta_w), 1)).field("peak_a", round(float(ev.peak_a), 3))
                   .field("inrush_ratio", round(float(ev.inrush_ratio), 2)).field("rise_s", round(float(ev.rise_s), 1))
                   .field("confidence", round(float(ev.confidence), 2))
                   .field("before_w", round(float(ev.before_w), 1)).field("after_w", round(float(ev.after_w), 1))
                   .time(ev.time, WritePrecision.S))
    for al in alerts:
        pts.append(Point("alert").tag("room", room).tag("rule", al.rule).tag("appliance", al.appliance)
                   .field("message", al.message).field("value", round(float(al.value), 2))
                   .field("duration_min", int(round(al.minutes))).field("active", int(al.active))
                   .time(al.start, WritePrecision.S))
    for iv in intervals:
        if iv.end is None:
            pts.append(Point("appliance_state").tag("room", room).tag("appliance", iv.appliance)
                       .field("on", 1).field("power_w", round(float(iv.power_w), 1))
                       .field("since_min", round((now - iv.start).total_seconds() / 60, 1))
                       .time(now, WritePrecision.S))
    p = Point("nilm_summary").tag("room", room).time(now, WritePrecision.S)
    for k, v in summary.items():
        if k != "per_appliance_wh":
            p.field(k, round(float(v), 2))
    p.field("window_h", round((now - start).total_seconds() / 3600, 2)).field("events", len(events))
    pts.append(p)

    client.write_api(write_options=SYNCHRONOUS).write(bucket=bucket, record=pts)


# ================================================================== commands

def analyze_room(client, clf, room: str, hours: float, write: bool = True) -> dict | None:
    now = pd.Timestamp.now(tz="UTC").floor("s")
    start = now - pd.Timedelta(hours=hours)
    df = fetch_power(client, room, start, now)
    if len(df) < 30:
        log.info("%s: not enough data (%d samples)", room, len(df))
        return None
    df = clean(df)
    events = detect_events(df)
    intervals = track(events, clf, df.index[0])
    table = allocate(df, intervals, now)
    alerts = evaluate_rules(room, table, intervals, now)
    summary = summarize(room, table)
    if write:
        write_results(client, room, start, now, events, intervals, table, alerts, summary)
        notify(alerts, room, now)

    on_now = [f"{iv.appliance} ({iv.power_w:.0f} W)" for iv in intervals if iv.end is None]
    per_app = ", ".join(f"{a} {wh:.0f}" for a, wh in sorted(summary["per_appliance_wh"].items(), key=lambda x: -x[1]))
    log.info("%s: %d events, %d alerts | total %.0f Wh = %s | standby %.0f Wh, unclassified %.0f Wh | "
             "efficiency %.0f%% | on now: %s", room, len(events), len(alerts), summary["total_wh"], per_app,
             summary["standby_wh"], summary["unclassified_wh"], summary["efficiency_pct"], ", ".join(on_now) or "-")
    return {"events": events, "intervals": intervals, "alerts": alerts, "summary": summary}


def cmd_run(args):
    with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG) as client:
        while True:
            clf = Classifier()  # reload in case the model was retrained
            try:
                rooms = [args.room] if args.room else list_rooms(client, args.hours)
                for room in rooms:
                    analyze_room(client, clf, room, args.hours)
                if not rooms:
                    log.info("No rooms with data yet")
            except Exception:
                log.exception("NILM pass failed")
            if args.once:
                break
            time.sleep(args.interval)


def cmd_label(args):
    """Record the signature of one appliance operated on its own during the last N minutes."""
    with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG) as client:
        now = datetime.now(timezone.utc)
        df = fetch_power(client, args.room, now - timedelta(minutes=args.minutes), now)
    if df.empty:
        raise SystemExit(f"No data for {args.room} in the last {args.minutes} min")
    ons = [ev for ev in detect_events(clean(df)) if ev.delta_w > 0]
    if not ons:
        raise SystemExit("No switch-on events found. Toggle the appliance ON (wait 10 s) and OFF a few times, then retry.")
    rows = [{**ev.features(), "label": args.appliance, "time": ev.time.isoformat(), "room": args.room} for ev in ons]
    for r in rows:
        print(f"  {r['time']}  +{r['abs_delta_w']:.0f} W  inrush x{r['inrush_ratio']:.2f}  rise {r['rise_s']:.0f} s")
    settings.DATA_DIR.mkdir(exist_ok=True)
    pd.DataFrame(rows).to_csv(SIGNATURES_CSV, mode="a", header=not SIGNATURES_CSV.exists(), index=False)
    print(f"Saved {len(rows)} '{args.appliance}' signatures to {SIGNATURES_CSV}")


def truth_signatures(client, room: str, hours: float) -> pd.DataFrame:
    """Match detected ON events to simulator ground truth and report detection quality."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours)
    df = fetch_power(client, room, start, now)
    truth = fetch_truth(client, room, start, now)
    if df.empty or truth.empty:
        raise SystemExit(f"No power data or truth_event rows for {room} (run simulate.py --backfill-hours first)")
    ons = [ev for ev in detect_events(clean(df)) if ev.delta_w > 0]
    truth_on = truth[truth["action"] == "on"].reset_index(drop=True)
    used, rows = set(), []
    for ev in ons:
        dt = (truth_on["_time"] - ev.time).abs().dt.total_seconds().to_numpy(copy=True)
        dt[list(used)] = math.inf  # each true event can be matched once
        k = int(np.argmin(dt))
        if dt[k] <= 15:
            used.add(k)
            rows.append({**ev.features(), "label": truth_on.loc[k, "appliance"], "time": ev.time.isoformat(), "room": room})
    print(f"Detection: {len(used)}/{len(truth_on)} true switch-ons found "
          f"(recall {len(used) / max(len(truth_on), 1):.0%}), {len(ons) - len(rows)} spurious "
          f"(precision {len(rows) / max(len(ons), 1):.0%})")
    return pd.DataFrame(rows)


def cmd_train(args):
    frames = []
    if SIGNATURES_CSV.exists():
        frames.append(pd.read_csv(SIGNATURES_CSV))
    if args.from_truth:
        with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG) as client:
            frames.append(truth_signatures(client, args.room, args.hours))
    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if data.empty:
        raise SystemExit("No labelled signatures. Use `nilm.py label ...` or `train --from-truth`.")
    counts = data["label"].value_counts()
    print("Signatures per appliance:\n" + counts.to_string())
    if len(counts) < 2:
        raise SystemExit("Need at least two appliances to train a classifier.")

    X, y = data[MODEL_FEATURES], data["label"]
    model = RandomForestClassifier(n_estimators=300, min_samples_leaf=1, class_weight="balanced", random_state=42)
    folds = int(min(5, counts.min()))
    if folds >= 2:
        cv = StratifiedKFold(n_splits=folds, shuffle=True, random_state=42)
        scores = cross_val_score(model, X, y, cv=cv)
        print(f"{folds}-fold cross-validated accuracy: {scores.mean():.1%} (+/- {scores.std():.1%})")
    else:
        print("Too few samples per class for cross-validation; training anyway.")
    model.fit(X, y)
    settings.DATA_DIR.mkdir(exist_ok=True)
    joblib.dump({"model": model, "features": MODEL_FEATURES, "trained": datetime.now(timezone.utc).isoformat()}, MODEL_PATH)
    print(f"Model saved to {MODEL_PATH}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="disaggregate, evaluate rules, write results")
    p.add_argument("--room", help="only this room (default: every room with data)")
    p.add_argument("--hours", type=float, default=24, help="analysis window (default 24)")
    p.add_argument("--interval", type=int, default=120, help="seconds between passes (default 120)")
    p.add_argument("--once", action="store_true")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("label", help="record signatures of one appliance operated alone")
    p.add_argument("--room", required=True)
    p.add_argument("--appliance", required=True, help="e.g. soldering_iron (names from appliances.json)")
    p.add_argument("--minutes", type=float, default=10)
    p.set_defaults(func=cmd_label)

    p = sub.add_parser("train", help="train the RandomForest event classifier")
    p.add_argument("--from-truth", action="store_true", help="also label events from simulator ground truth")
    p.add_argument("--room", default="sim-room")
    p.add_argument("--hours", type=float, default=24)
    p.set_defaults(func=cmd_train)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
