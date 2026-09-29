#pragma once
// Hardware + measurement settings. Adjust to match your build.

// ---- ACS712 -----------------------------------------------------------------
// ACS712 OUT -> 1k -> (node A) -> 2k -> GND.  Node A -> GPIO34, 100nF to GND.
#define CURRENT_ADC_PIN 34            // ADC1 channel (ADC2 pins don't work with Wi-Fi on)

// Sensor sensitivity: 5A = 185, 20A = 100, 30A = 66 (mV per amp at the sensor output)
#define ACS712_MV_PER_AMP 100.0f

// Voltage divider scaling: 2k / (1k + 2k). 5V sensor swing -> 3.33V at the ADC.
#define DIVIDER_RATIO (2000.0f / (1000.0f + 2000.0f))

// Multiply the measured current by this after comparing against a clamp meter.
#define CURRENT_CALIBRATION 1.0f

// Readings below this are treated as 0 A (ESP32 ADC noise floor, spec: <100 mA clamp).
#define NOISE_FLOOR_AMPS 0.10f

// ---- Sampling ---------------------------------------------------------------
#define SAMPLE_RATE_HZ 2000           // fixed-rate burst sampling (spec: 1-2 kHz)
#define MAINS_FREQUENCY_HZ 50         // 50 or 60
#define RMS_CYCLES 10                 // one RMS window = 10 cycles (200 ms at 50 Hz)

// ---- Mains ------------------------------------------------------------------
// No voltage sensor: apparent power S = nominal V x Irms; "power_w" = S x assumed PF.
#define MAINS_VOLTAGE 230.0f
#define ASSUMED_POWER_FACTOR 1.0f

// ---- Local UI ---------------------------------------------------------------
#define I2C_SDA_PIN 21
#define I2C_SCL_PIN 22
#define LCD_I2C_ADDR 0x27             // 0x3F on some backpacks (blank screen = wrong address)

#define SERVO_PIN 18
#define SERVO_FULL_SCALE_VA 2300.0f   // 180 deg on the dial (= 10 A at 230 V)
#define SERVO_REVERSED false          // true if the needle sweeps the wrong way on your dial face

#define LED_PIN 2
#define ALERT_CURRENT_A 8.0f          // facility safety threshold -> LED pulses, LCD "OVERLOAD"

// ---- Telemetry --------------------------------------------------------------
#define PUBLISH_INTERVAL_MS 1000      // spec: 0.5-1 Hz
#define META_INTERVAL_MS 10000        // RSSI/uptime included every 10 s (0.1 Hz)
// Topics: <SITE_ID>/<ROOM_ID>/power and <SITE_ID>/<ROOM_ID>/status (see secrets.h)
