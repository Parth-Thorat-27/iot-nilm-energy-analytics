// Over-current cutoff: ESP32 + ACS712 + 16x2 I2C LCD + SG90 servo + status LED
//
// Pins:  ACS712 divider node A -> GPIO34   LCD SDA -> 21, SCL -> 22
//        Servo signal -> GPIO18            Status LED -> GPIO2
//        BOOT button (GPIO0, on the board) -> reset a latched trip
//
// Current is measured as true RMS over whole mains cycles. The window mean is
// subtracted from every sample, so the ACS712's zero offset (Vcc/2, which drifts
// with USB supply voltage) cancels out without a hard-coded 2500 mV.

#include <Wire.h>
#include <LiquidCrystal_I2C.h>
#include <ESP32Servo.h>
#include <math.h>

// --- Pins ---
const int ACS712_PIN = 34;   // ADC1 channel (works with Wi-Fi on)
const int SERVO_PIN  = 18;
const int LED_PIN    = 2;
const int RESET_PIN  = 0;    // on-board BOOT button, active LOW

// --- Sensor ---
// Divider: 1k top, 2k bottom -> sensor voltage = GPIO34 voltage x (1k + 2k) / 2k
const float DIVIDER_RATIO = 1.5f;
// mV per amp: 5A = 185, 20A = 100, 30A = 66
const float ACS_SENSITIVITY_MV = 100.0f;
// Multiply by (clamp-meter amps / displayed amps) after checking a known load
const float CALIBRATION = 1.0f;
// Readings below this are ADC noise
const float NOISE_FLOOR_A = 0.12f;

// --- Mains ---
const int MAINS_HZ   = 50;   // 60 in the Americas
const int RMS_CYCLES = 10;   // 10 cycles = 200 ms window at 50 Hz

// --- Trip ---
const float TRIP_AMPS         = 2.0f;
const int   TRIP_CONFIRM      = 2;    // consecutive over-limit readings before tripping (ignores inrush)
const int   SERVO_HOME_DEG    = 0;
const int   SERVO_TRIPPED_DEG = 90;

LiquidCrystal_I2C lcd(0x27, 16, 2);  // try 0x3F if the screen stays blank
Servo cutoffServo;

bool tripped = false;
int overCount = 0;
float tripAmps = 0.0f;

float readIrms() {
  const uint32_t windowUs = (1000000UL / MAINS_HZ) * RMS_CYCLES;
  double sum = 0.0, sumSq = 0.0;
  uint32_t n = 0;

  const uint32_t start = micros();
  while (micros() - start < windowUs) {
    const double mv = analogReadMilliVolts(ACS712_PIN);  // factory-calibrated, unlike raw/4095*3.3
    sum += mv;
    sumSq += mv * mv;
    n++;
  }
  if (n == 0) return 0.0f;

  const double mean = sum / n;
  const double rmsMvAtPin = sqrt(fmax(sumSq / n - mean * mean, 0.0));
  const float amps = (float)(rmsMvAtPin * DIVIDER_RATIO / ACS_SENSITIVITY_MV) * CALIBRATION;
  return amps < NOISE_FLOOR_A ? 0.0f : amps;
}

void setTripped(bool on) {
  tripped = on;
  digitalWrite(LED_PIN, on ? HIGH : LOW);
  cutoffServo.write(on ? SERVO_TRIPPED_DEG : SERVO_HOME_DEG);
}

void setup() {
  Serial.begin(115200);

  pinMode(LED_PIN, OUTPUT);
  pinMode(RESET_PIN, INPUT_PULLUP);
  analogReadResolution(12);
  analogSetPinAttenuation(ACS712_PIN, ADC_11db);  // ~0-3.1 V range

  cutoffServo.setPeriodHertz(50);
  cutoffServo.attach(SERVO_PIN, 500, 2400);

  // Power-on self-test: 3 LED blinks + servo to trip position and back, to check wiring.
  Serial.println("[self-test] LED blink x3, servo 0 -> 90 -> 0");
  for (int i = 0; i < 3; i++) {
    digitalWrite(LED_PIN, HIGH);
    delay(150);
    digitalWrite(LED_PIN, LOW);
    delay(150);
  }
  cutoffServo.write(SERVO_TRIPPED_DEG);
  delay(800);
  setTripped(false);
  delay(800);

  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcd.clear();
  lcd.print("System Init...");
  delay(1000);
  lcd.clear();
}

void loop() {
  const float amps = readIrms();

  if (!tripped) {
    overCount = (amps >= TRIP_AMPS) ? overCount + 1 : 0;
    if (overCount >= TRIP_CONFIRM) {
      tripAmps = amps;
      setTripped(true);  // latches: the load is now cut, so current drops to 0 -- don't auto-rearm
      Serial.printf("TRIPPED at %.2f A\n", tripAmps);
    }
  } else if (digitalRead(RESET_PIN) == LOW) {
    overCount = 0;
    setTripped(false);
    Serial.println("Trip reset");
  }

  char line[17];
  lcd.setCursor(0, 0);
  snprintf(line, sizeof(line), "I: %6.2f A     ", amps);
  lcd.print(line);
  lcd.setCursor(0, 1);
  if (tripped) {
    snprintf(line, sizeof(line), "TRIP %.1fA  BOOT ", tripAmps);  // BOOT = press to reset
  } else {
    snprintf(line, sizeof(line), "STATUS: NORMAL  ");
  }
  lcd.print(line);

  Serial.printf("Irms: %.3f A | %s\n", amps, tripped ? "TRIPPED" : "normal");
  delay(300);
}
