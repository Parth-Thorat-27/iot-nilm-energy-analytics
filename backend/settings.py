"""Shared settings, loaded from the project-root .env file plus rooms.json / appliances.json."""

import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BACKEND_DIR = Path(__file__).resolve().parent
DATA_DIR = BACKEND_DIR / "data"
load_dotenv(BACKEND_DIR.parent / ".env")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "+/+/power")  # <site>/<room>/power
MQTT_USER = os.getenv("MQTT_USER") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

INFLUXDB_URL = os.getenv("INFLUXDB_URL", "http://localhost:8086")
INFLUXDB_TOKEN = os.environ["INFLUXDB_TOKEN"]
INFLUXDB_ORG = os.getenv("INFLUXDB_ORG", "iota")
INFLUXDB_BUCKET = os.getenv("INFLUXDB_BUCKET", "energy")

TZ = ZoneInfo(os.getenv("TZ_NAME", "Asia/Kolkata"))
ALERT_WEBHOOK_URL = os.getenv("ALERT_WEBHOOK_URL") or None

MEASUREMENT = "power"
ANOMALY_MEASUREMENT = "anomaly"


def _load(name: str) -> dict:
    data = json.loads((BACKEND_DIR / name).read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_") or k == "_default"}


ROOMS = _load("rooms.json")
APPLIANCES = {k: v for k, v in _load("appliances.json").items() if k != "_default"}


def room_config(room: str) -> dict:
    return {**ROOMS["_default"], **ROOMS.get(room, {})}
