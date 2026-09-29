"""MQTT -> InfluxDB ingestion daemon.

Subscribes to <site>/<room>/power (e.g. hostel/room101/power), validates each
JSON frame, injects metadata tags (site, room, phase from rooms.json, device),
and writes it to the `power` measurement. <site>/<room>/status (online/offline,
set by the node's MQTT last-will) goes to `node_status`.

    python bridge.py
"""

import json
import logging
import math
import signal
import sys
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

import settings

log = logging.getLogger("bridge")

FLOAT_FIELDS = ("current_a", "apparent_va", "power_w", "peak_a", "energy_wh", "voltage_v")
INT_FIELDS = ("rssi", "uptime_s", "samples")
MAX_CURRENT_A = 50.0  # anything above this is a corrupt frame, not a reading

influx = InfluxDBClient(url=settings.INFLUXDB_URL, token=settings.INFLUXDB_TOKEN, org=settings.INFLUXDB_ORG)
write_api = influx.write_api(write_options=SYNCHRONOUS)


def parse_topic(topic: str) -> tuple[str, str]:
    parts = topic.split("/")
    if len(parts) != 3:
        raise ValueError(f"expected <site>/<room>/<kind>, got {topic!r}")
    return parts[0], parts[1]


def validate(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("payload is not a JSON object")
    current = payload.get("current_a")
    if not isinstance(current, (int, float)) or math.isnan(current) or not 0 <= current <= MAX_CURRENT_A:
        raise ValueError(f"current_a out of range: {current!r}")
    for key in FLOAT_FIELDS + INT_FIELDS:
        if key in payload and not isinstance(payload[key], (int, float)):
            raise ValueError(f"{key} is not numeric: {payload[key]!r}")


def to_point(topic: str, payload: dict) -> Point:
    site, room = parse_topic(topic)
    meta = settings.room_config(room)
    point = (
        Point(settings.MEASUREMENT)
        .tag("site", site)
        .tag("room", room)
        .tag("phase", meta["phase"])
        .tag("device", str(payload.get("device", room)))
    )
    for key in FLOAT_FIELDS:
        if key in payload:
            point.field(key, float(payload[key]))
    if "apparent_va" not in payload and "power_w" in payload:
        point.field("apparent_va", float(payload["power_w"]))
    for key in INT_FIELDS:
        if key in payload:
            point.field(key, int(payload[key]))
    # ESP32 has no RTC/NTP, so timestamp on arrival.
    return point.time(datetime.now(timezone.utc), WritePrecision.MS)


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        log.error("MQTT connect failed: %s", reason_code)
        return
    status_topic = settings.MQTT_TOPIC.rsplit("/", 1)[0] + "/status"
    log.info("Connected to MQTT %s:%s, subscribing to %s and %s",
             settings.MQTT_HOST, settings.MQTT_PORT, settings.MQTT_TOPIC, status_topic)
    client.subscribe(settings.MQTT_TOPIC, qos=0)
    client.subscribe(status_topic, qos=1)


def on_status(topic: str, text: str):
    site, room = parse_topic(topic)
    log.info("%s/%s is %s", site, room, text)
    point = (
        Point("node_status").tag("site", site).tag("room", room)
        .field("online", 1 if text == "online" else 0)
        .time(datetime.now(timezone.utc), WritePrecision.MS)
    )
    write_api.write(bucket=settings.INFLUXDB_BUCKET, record=point)


def on_message(client, userdata, msg):
    try:
        if msg.topic.endswith("/status"):
            on_status(msg.topic, msg.payload.decode(errors="replace"))
            return
        payload = json.loads(msg.payload)
        validate(payload)
        point = to_point(msg.topic, payload)
        write_api.write(bucket=settings.INFLUXDB_BUCKET, record=point)
        log.info("%-22s %6.3f A  %7.1f VA", msg.topic, payload["current_a"], payload.get("apparent_va", payload.get("power_w", 0)))
    except (ValueError, TypeError) as e:
        log.warning("Rejected frame on %s: %s (%r)", msg.topic, e, msg.payload[:200])
    except Exception:
        log.exception("Failed to write to InfluxDB")


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="iota-bridge")
    if settings.MQTT_USER:
        client.username_pw_set(settings.MQTT_USER, settings.MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=30)

    def shutdown(*_):
        log.info("Shutting down")
        client.disconnect()
        influx.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    client.connect(settings.MQTT_HOST, settings.MQTT_PORT, keepalive=30)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
