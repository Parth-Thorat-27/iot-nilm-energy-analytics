"""Export collected data for one room to Excel (one sheet per dataset) or CSV files.

    python export.py --room room101                                  # last 24 h -> exports/room101_....xlsx
    python export.py --room room101 --from 2026-09-28 --to 2026-10-05
    python export.py --room room101 --hours 72 --resample 1min       # downsample raw readings
    python export.py --room room101 --format csv                     # folder of CSVs instead

Times are local (TZ_NAME in .env). Sheets/files:
    readings        raw 1 Hz telemetry (current, apparent power, peak, energy, RSSI...)
    appliance_power NILM per-appliance power, 1-min, one column per appliance
    events          detected ON/OFF switching events with their features
    alerts          wastage alerts (night draw, phantom load, runaway)
    anomalies       IsolationForest anomaly scores
    summary         energy per appliance for the exported range
    signatures      labelled training signatures (data/signatures.csv), if any
"""

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from influxdb_client import InfluxDBClient

import settings

EXCEL_MAX_ROWS = 1_048_575
EXPORT_DIR = settings.BACKEND_DIR.parent / "exports"


def query(client, measurement: str, room: str, start: datetime, stop: datetime, row_key: list[str]) -> pd.DataFrame:
    keys = ", ".join(f'"{k}"' for k in row_key)
    q = f'''from(bucket: "{settings.INFLUXDB_BUCKET}")
  |> range(start: {start.strftime("%Y-%m-%dT%H:%M:%SZ")}, stop: {stop.strftime("%Y-%m-%dT%H:%M:%SZ")})
  |> filter(fn: (r) => r._measurement == "{measurement}" and r.room == "{room}")
  |> pivot(rowKey: [{keys}], columnKey: ["_field"], valueColumn: "_value")
  |> group()
  |> sort(columns: ["_time"])'''
    df = client.query_api().query_data_frame(q)
    if isinstance(df, list):
        df = pd.concat(df, ignore_index=True) if df else pd.DataFrame()
    if df.empty:
        return df
    df = df.drop(columns=["result", "table", "_start", "_stop", "_measurement"], errors="ignore")
    df["_time"] = df["_time"].dt.tz_convert(settings.TZ).dt.tz_localize(None)  # Excel has no timezones
    return df.rename(columns={"_time": "time"})


def build(client, room: str, start: datetime, stop: datetime, resample: str | None) -> dict[str, pd.DataFrame]:
    sheets = {}

    readings = query(client, "power", room, start, stop, ["_time"])
    if not readings.empty:
        cols = ["time", "device", "current_a", "apparent_va", "power_w", "peak_a", "energy_wh", "voltage_v", "rssi", "uptime_s", "samples"]
        readings = readings[[c for c in cols if c in readings.columns]]
        if resample:
            num = readings.select_dtypes("number").columns
            readings = readings.set_index("time")[num].resample(resample).mean().dropna(how="all").reset_index()
    sheets["readings"] = readings

    ap = query(client, "appliance_power", room, start, stop, ["_time", "appliance"])
    if not ap.empty:
        ap = ap.pivot_table(index="time", columns="appliance", values="power_w").reset_index()
        ap.columns.name = None
    sheets["appliance_power"] = ap

    ev = query(client, "nilm_event", room, start, stop, ["_time", "appliance", "action", "method"])
    if not ev.empty:
        cols = ["time", "action", "appliance", "delta_w", "before_w", "after_w", "rise_s", "inrush_ratio", "peak_a", "confidence", "method"]
        ev = ev[[c for c in cols if c in ev.columns]]
    sheets["events"] = ev

    al = query(client, "alert", room, start, stop, ["_time", "rule", "appliance"])
    if not al.empty:
        cols = ["time", "rule", "appliance", "message", "value", "duration_min", "active"]
        al = al[[c for c in cols if c in al.columns]]
    sheets["alerts"] = al

    an = query(client, "anomaly", room, start, stop, ["_time"])
    if not an.empty:
        an = an[[c for c in ["time", "power_w", "score", "is_anomaly"] if c in an.columns]]
    sheets["anomalies"] = an

    if not ap.empty:
        energy = (ap.drop(columns="time").sum() / 60.0).rename("energy_wh")  # 1-min mean W -> Wh
        summary = energy.reset_index().rename(columns={"index": "appliance"})
        summary["energy_kwh"] = summary["energy_wh"] / 1000
        summary["share_pct"] = 100 * summary["energy_wh"] / summary["energy_wh"].sum()
        sheets["summary"] = summary.sort_values("energy_wh", ascending=False).round(3)
    else:
        sheets["summary"] = pd.DataFrame()

    sig = settings.DATA_DIR / "signatures.csv"
    sheets["signatures"] = pd.read_csv(sig) if sig.exists() else pd.DataFrame()
    return sheets


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--room", required=True)
    parser.add_argument("--from", dest="date_from", help="start date YYYY-MM-DD (local)")
    parser.add_argument("--to", dest="date_to", help="end date YYYY-MM-DD (local, inclusive)")
    parser.add_argument("--hours", type=float, default=24, help="if no --from: export the last N hours (default 24)")
    parser.add_argument("--resample", help="downsample raw readings, e.g. 10s, 1min, 15min")
    parser.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    parser.add_argument("--out", type=Path, default=EXPORT_DIR)
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    if args.date_from:
        start = datetime.fromisoformat(args.date_from).replace(tzinfo=settings.TZ)
        stop = (datetime.fromisoformat(args.date_to).replace(tzinfo=settings.TZ) + timedelta(days=1)) if args.date_to else now
    else:
        start, stop = now - timedelta(hours=args.hours), now
    start, stop = start.astimezone(timezone.utc), min(stop.astimezone(timezone.utc), now)

    with InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG, timeout=300_000) as client:
        sheets = build(client, args.room, start, stop, args.resample)

    if all(df.empty for name, df in sheets.items() if name != "signatures"):
        raise SystemExit(f"No data for room '{args.room}' between {start:%Y-%m-%d %H:%M} and {stop:%Y-%m-%d %H:%M} UTC")

    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{args.room}_{start.astimezone(settings.TZ):%Y%m%d-%H%M}_{stop.astimezone(settings.TZ):%Y%m%d-%H%M}"
    if args.format == "xlsx":
        too_big = [n for n, df in sheets.items() if len(df) > EXCEL_MAX_ROWS]
        if too_big:
            raise SystemExit(f"{too_big} exceed Excel's row limit - use --resample 10s (or 1min) or --format csv")
        path = args.out / f"{stem}.xlsx"
        with pd.ExcelWriter(path, engine="openpyxl") as xw:
            for name, df in sheets.items():
                (df if not df.empty else pd.DataFrame({"note": ["no data in this range"]})).to_excel(xw, sheet_name=name, index=False)
    else:
        path = args.out / stem
        path.mkdir(exist_ok=True)
        for name, df in sheets.items():
            if not df.empty:
                df.to_csv(path / f"{name}.csv", index=False)

    for name, df in sheets.items():
        print(f"  {name:16} {len(df):>8} rows")
    print(f"Exported to {path}")


if __name__ == "__main__":
    main()
