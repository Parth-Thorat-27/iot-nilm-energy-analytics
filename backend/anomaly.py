"""Unsupervised anomaly detection on power readings (IsolationForest).

Each cycle, per room:
  1. Pull the last TRAIN_DAYS of `power_w`, aggregated to 1-minute windows in Flux.
  2. Build features: mean / min / max / spread per minute, change vs previous
     minute, and time-of-day (as sin/cos so 23:59 is close to 00:00).
  3. Fit an IsolationForest and score the most recent SCORE_MINUTES.
  4. Write `anomaly` points (score, is_anomaly, power_w) back to InfluxDB.
     score > 0 means anomalous; rewriting the same timestamps is idempotent.

    python anomaly.py            # loop every 5 minutes
    python anomaly.py --once     # single pass
    python anomaly.py --once --score-minutes 1440   # also score the past day
"""

import argparse
import logging
import time

import numpy as np
import pandas as pd
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS
from sklearn.ensemble import IsolationForest

import settings

log = logging.getLogger("anomaly")

TRAIN_DAYS = 7
SCORE_MINUTES = 15
MIN_TRAIN_ROWS = 60  # need at least an hour of 1-minute windows
CONTAMINATION = 0.01  # expected fraction of anomalous minutes
FEATURES = ["mean", "min", "max", "spread", "delta", "hour_sin", "hour_cos"]

QUERY = """
data = from(bucket: "{bucket}")
  |> range(start: -{days}d)
  |> filter(fn: (r) => r._measurement == "{measurement}" and r._field == "power_w")
  |> group(columns: ["room"])

mean = data |> aggregateWindow(every: 1m, fn: mean, createEmpty: false) |> set(key: "_field", value: "mean")
mn   = data |> aggregateWindow(every: 1m, fn: min,  createEmpty: false) |> set(key: "_field", value: "min")
mx   = data |> aggregateWindow(every: 1m, fn: max,  createEmpty: false) |> set(key: "_field", value: "max")

union(tables: [mean, mn, mx])
  |> group(columns: ["room"])
  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")
  |> keep(columns: ["_time", "room", "mean", "min", "max"])
"""


def fetch(client: InfluxDBClient) -> pd.DataFrame:
    query = QUERY.format(bucket=settings.INFLUXDB_BUCKET, measurement=settings.MEASUREMENT, days=TRAIN_DAYS)
    df = client.query_api().query_data_frame(query)
    if isinstance(df, list):  # one frame per table
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    if df.empty:
        return df
    return df.drop(columns=["result", "table"], errors="ignore").sort_values(["room", "_time"])


def featurize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["spread"] = df["max"] - df["min"]
    df["delta"] = df["mean"].diff().fillna(0.0)
    hours = df["_time"].dt.tz_convert(None).dt.hour + df["_time"].dt.minute / 60.0
    df["hour_sin"] = np.sin(2 * np.pi * hours / 24)
    df["hour_cos"] = np.cos(2 * np.pi * hours / 24)
    return df


def score_room(room: str, df: pd.DataFrame, score_minutes: int) -> list[Point]:
    df = featurize(df)
    if len(df) < MIN_TRAIN_ROWS:
        log.info("%s: only %d minutes of data, need %d to train - skipping", room, len(df), MIN_TRAIN_ROWS)
        return []

    model = IsolationForest(n_estimators=200, contamination=CONTAMINATION, random_state=42)
    model.fit(df[FEATURES])

    cutoff = df["_time"].max() - pd.Timedelta(minutes=score_minutes)
    recent = df[df["_time"] > cutoff]
    # decision_function: negative = anomaly. Flip so positive = anomaly.
    scores = -model.decision_function(recent[FEATURES])

    points = []
    for (_, row), score in zip(recent.iterrows(), scores):
        points.append(
            Point(settings.ANOMALY_MEASUREMENT)
            .tag("room", room)
            .field("score", float(score))
            .field("is_anomaly", int(score > 0))
            .field("power_w", float(row["mean"]))
            .time(row["_time"].to_pydatetime(), WritePrecision.S)
        )
    flagged = int((scores > 0).sum())
    log.info("%s: trained on %d min, scored %d min, %d anomalous", room, len(df), len(recent), flagged)
    return points


def run_once(client: InfluxDBClient, score_minutes: int = SCORE_MINUTES):
    df = fetch(client)
    if df.empty:
        log.info("No power data yet")
        return
    write_api = client.write_api(write_options=SYNCHRONOUS)
    for room, group in df.groupby("room"):
        points = score_room(room, group.reset_index(drop=True), score_minutes)
        if points:
            write_api.write(bucket=settings.INFLUXDB_BUCKET, record=points)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--once", action="store_true", help="run a single pass and exit")
    parser.add_argument("--score-minutes", type=int, default=SCORE_MINUTES, help="how far back to score (default 15; use 1440 to score the last day)")
    parser.add_argument("--interval", type=int, default=300, help="seconds between passes (default 300)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG) as client:
        while True:
            try:
                run_once(client, args.score_minutes)
            except Exception:
                log.exception("Anomaly pass failed")
            if args.once:
                break
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
