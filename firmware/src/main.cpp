// NILM edge node: ESP32 + ACS712 -> true-RMS current, apparent power, inrush peak
// -> MQTT JSON on <SITE_ID>/<ROOM_ID>/power, plus local LCD / servo dial / alert LED.
//
// Sampling is non-blocking and fixed-rate (SAMPLE_RATE_HZ). Samples accumulate into
// RMS windows of RMS_CYCLES whole mains cycles. The window mean is subtracted, so the
// ACS712 zero offset (Vcc/2, drifts with supply) cancels without calibration.
// Slow work (Wi-Fi, MQTT, I2C LCD, servo) runs only between windows, so it never
// punches gaps into a measurement.

#include <Arduino.h>
#include <WiFi.h>
#include <Wire.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>
#include <math.h>

#include "config.h"
#include "secrets.h"

static const uint32_t SAMPLE_PERIOD_US = 1000000UL / SAMPLE_RATE_HZ;
static const uint32_t WINDOW_US = (1000000UL / MAINS_FREQUENCY_HZ) * RMS_CYCLES;
static const float MV_TO_AMPS = 1.0f / (DIVIDER_RATIO * ACS712_MV_PER_AMP) * CURRENT_CALIBRATION;

static WiFiClient wifiClient;
static PubSubClient mqtt(wifiClient);
static LiquidCrystal_I2C lcd(LCD_I2C_ADDR, 16, 2);
static Servo dial;

static const String powerTopic = String(SITE_ID) + "/" + ROOM_ID + "/power";
static const String statusTopic = String(SITE_ID) + "/" + ROOM_ID + "/status";

// Current RMS window
struct Window {
  double sum = 0, sumSq = 0;
  float minMv = 1e9f, maxMv = -1e9f;
  uint32_t n = 0, startUs = 0, nextSampleUs = 0;
};
static Window win;

// Aggregate of windows since the last publish
static double sumIrmsSq = 0;
static uint32_t windowsSincePublish = 0, samplesSincePublish = 0;
static float peakSincePublish = 0;

static float lastIrms = 0, lastApparentVa = 0;
static double energyWh = 0;
static uint32_t lastPublishMs = 0, lastMetaMs = 0, lastEnergyMs = 0;
static uint32_t lastWifiAttemptMs = 0, lastMqttAttemptMs = 0;
static int lastDialDeg = -1;
static bool ledOn = false;

// ---------------------------------------------------------------- sampling --

static void startWindow() {
  win = Window();
  win.startUs = micros();
  win.nextSampleUs = win.startUs;
}

// Takes at most one sample per call. Returns true when the window is complete.
static bool sampleTick() {
  const uint32_t now = micros();
  if ((int32_t)(now - win.nextSampleUs) >= 0) {
    const float mv = analogReadMilliVolts(CURRENT_ADC_PIN);
    win.sum += mv;
    win.sumSq += (double)mv * mv;
    win.minMv = fminf(win.minMv, mv);
    win.maxMv = fmaxf(win.maxMv, mv);
    win.n++;
    win.nextSampleUs += SAMPLE_PERIOD_US;
    // If something stalled us, resync instead of bursting to catch up.
    if ((int32_t)(now - win.nextSampleUs) > (int32_t)SAMPLE_PERIOD_US) win.nextSampleUs = now + SAMPLE_PERIOD_US;
  }
  return now - win.startUs >= WINDOW_US;
}

static void finishWindow() {
  if (win.n < 10) return;
  const double mean = win.sum / win.n;
  const double rmsMv = sqrt(fmax(win.sumSq / win.n - mean * mean, 0.0));
  float irms = rmsMv * MV_TO_AMPS;
  if (irms < NOISE_FLOOR_AMPS) irms = 0;
  // Largest instantaneous excursion from the offset -> inrush / crest feature.
  const float peakA = fmaxf(win.maxMv - mean, mean - win.minMv) * MV_TO_AMPS;

  sumIrmsSq += (double)irms * irms;
  windowsSincePublish++;
  samplesSincePublish += win.n;
  if (irms > 0) peakSincePublish = fmaxf(peakSincePublish, peakA);
}

// ------------------------------------------------------------ connectivity --

static void ensureWifi() {
  if (WiFi.status() == WL_CONNECTED) return;
  if (lastWifiAttemptMs != 0 && millis() - lastWifiAttemptMs < 10000) return;
  lastWifiAttemptMs = millis();
  Serial.printf("[wifi] connecting to %s ...\n", WIFI_SSID);
  WiFi.disconnect();
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

static void ensureMqtt() {
  if (WiFi.status() != WL_CONNECTED || mqtt.connected()) return;
  if (lastMqttAttemptMs != 0 && millis() - lastMqttAttemptMs < 5000) return;
  lastMqttAttemptMs = millis();

  Serial.printf("[mqtt] connecting to %s:%d ...\n", MQTT_HOST, MQTT_PORT);
  const char *user = strlen(MQTT_USER) ? MQTT_USER : nullptr;
  const char *pass = strlen(MQTT_PASSWORD) ? MQTT_PASSWORD : nullptr;
  if (mqtt.connect(DEVICE_ID, user, pass, statusTopic.c_str(), 1, true, "offline")) {
    Serial.println("[mqtt] connected");
    mqtt.publish(statusTopic.c_str(), "online", true);
  } else {
    Serial.printf("[mqtt] failed, state=%d\n", mqtt.state());
  }
}

// --------------------------------------------------------------- telemetry --

static void publish() {
  const uint32_t now = millis();
  lastIrms = windowsSincePublish ? sqrt(sumIrmsSq / windowsSincePublish) : 0;
  lastApparentVa = MAINS_VOLTAGE * lastIrms;
  const float powerW = lastApparentVa * ASSUMED_POWER_FACTOR;

  energyWh += powerW * ((now - lastEnergyMs) / 3600000.0);
  lastEnergyMs = now;

  JsonDocument doc;
  doc["device"] = DEVICE_ID;
  doc["room"] = ROOM_ID;
  doc["current_a"] = roundf(lastIrms * 1000) / 1000;
  doc["apparent_va"] = roundf(lastApparentVa * 10) / 10;
  doc["power_w"] = roundf(powerW * 10) / 10;
  doc["peak_a"] = roundf(peakSincePublish * 1000) / 1000;
  doc["energy_wh"] = round(energyWh * 100) / 100;
  doc["voltage_v"] = MAINS_VOLTAGE;
  doc["samples"] = samplesSincePublish;
  if (now - lastMetaMs >= META_INTERVAL_MS || lastMetaMs == 0) {
    lastMetaMs = now;
    doc["rssi"] = WiFi.RSSI();
    doc["uptime_s"] = now / 1000;
  }

  char payload[320];
  const size_t len = serializeJson(doc, payload, sizeof(payload));
  if (mqtt.connected()) {
    const bool ok = mqtt.publish(powerTopic.c_str(), (const uint8_t *)payload, len, false);
    Serial.printf("[pub%s] %s\n", ok ? "" : " FAILED", payload);
  } else {
    Serial.printf("[offline] %s\n", payload);
  }

  sumIrmsSq = 0;
  windowsSincePublish = samplesSincePublish = 0;
  peakSincePublish = 0;
}

// ---------------------------------------------------------------- local UI --

static void updateUi() {
  const bool overload = lastIrms >= ALERT_CURRENT_A;

  // LED: pulse (toggle every window, ~2.5 Hz) while over the safety threshold.
  ledOn = overload ? !ledOn : false;
  digitalWrite(LED_PIN, ledOn);

  // Servo analog dial: 0..180 deg over 0..SERVO_FULL_SCALE_VA. Skip tiny moves (jitter).
  const int deg = (int)constrain(lastApparentVa / SERVO_FULL_SCALE_VA * 180.0f, 0.0f, 180.0f);
  if (abs(deg - lastDialDeg) >= 2) {
    dial.write(SERVO_REVERSED ? 180 - deg : deg);
    lastDialDeg = deg;
  }
}

static void updateLcd() {
  char line[17];
  snprintf(line, sizeof(line), "I%5.2fA S%5.0fVA", lastIrms, lastApparentVa);
  lcd.setCursor(0, 0);
  lcd.print(line);

  const char *state = lastIrms >= ALERT_CURRENT_A ? "OVERLOAD" : lastIrms > 0 ? "ACTIVE" : "IDLE";
  const char *net = mqtt.connected() ? "MQTT OK" : WiFi.status() == WL_CONNECTED ? "WIFI OK" : "NO NET";
  snprintf(line, sizeof(line), "%-9s%7s", state, net);
  lcd.setCursor(0, 1);
  lcd.print(line);
}

// ------------------------------------------------------------------- main --

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("\nNILM edge node '%s' -> %s\n", DEVICE_ID, powerTopic.c_str());

  pinMode(LED_PIN, OUTPUT);
  analogReadResolution(12);
  analogSetPinAttenuation(CURRENT_ADC_PIN, ADC_11db);  // ~0-3.1 V input range

  Wire.begin(I2C_SDA_PIN, I2C_SCL_PIN);
  lcd.init();
  lcd.backlight();
  lcd.print("NILM node");
  lcd.setCursor(0, 1);
  lcd.print(ROOM_ID);

  dial.setPeriodHertz(50);
  dial.attach(SERVO_PIN, 500, 2400);
  // Power-on self-test: 3 LED blinks + full dial sweep, so LED/servo wiring can be checked at a glance.
  Serial.println("[self-test] LED blink x3, servo sweep 0 -> 180 -> 0");
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(150);
    digitalWrite(LED_PIN, LOW);
    delay(150);
  }
  dial.write(SERVO_REVERSED ? 0 : 180);
  delay(800);
  dial.write(SERVO_REVERSED ? 180 : 0);  // needle at zero
  delay(800);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  ensureWifi();

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setBufferSize(512);
  mqtt.setKeepAlive(30);
  mqtt.setSocketTimeout(2);  // bound the stall a failed connect can cause

  lastEnergyMs = lastPublishMs = millis();
  startWindow();
}

void loop() {
  if (!sampleTick()) return;  // mid-window: only sample

  // Window complete: do all slow work in the gap, then start the next window.
  finishWindow();
  updateUi();

  ensureWifi();
  ensureMqtt();
  mqtt.loop();

  if (millis() - lastPublishMs >= PUBLISH_INTERVAL_MS) {
    lastPublishMs = millis();
    publish();
    updateLcd();  // I2C LCD writes take ~10 ms; once a second is plenty
  }
  startWindow();
}
