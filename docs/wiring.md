# Wiring

> **⚠ Mains voltage can kill.** Build and flash with the extension cable **unplugged**.
> Once mains is connected, treat the whole board as live: close the enclosure before
> plugging in, and never touch the board, terminals or USB cable while it's powered.
> If you're not comfortable working with 230 V, get someone qualified to do the mains side.

## Mains side (inside the ABS enclosure)

Only the **live** conductor goes through the fuse and the ACS712. Neutral and earth pass straight through.

```
 Wall plug end                                               Socket end
 ───────────                                                 ──────────
 LIVE (brown) ──[ 10 A fuse ]──► ACS712 IP+   IP- ─────────► LIVE (brown)
 NEUTRAL (blue) ───────────────[ screw terminal ]──────────► NEUTRAL (blue)
 EARTH (green/yellow) ─────────[ screw terminal ]──────────► EARTH (green/yellow)
```

- Use the screw terminal blocks to join conductors. Don't use jumper wires or the breadboard on the mains side.
- The 10 A fuse also caps the current below the ACS712-20A's range.
- Keep mains wiring physically separated from the low-voltage side, and add strain relief where the cable enters the box.

## Low-voltage side

```
            ACS712 module                        ESP32 DevKit V1
           ┌─────────────┐                     ┌───────────────┐
           │ VCC ────────┼─────────────────────┤ VIN (5 V)     │
           │ GND ────────┼──────────┬──────────┤ GND           │
           │ OUT ──┐     │          │          │               │
           └───────┼─────┘          │          │               │
                   │                │          │               │
                 [1 kΩ]             │          │               │
                   │                │          │               │
                   ├────────────────┼──────────┤ GPIO34 (ADC)  │
                   │         │      │          └───────────────┘
                 [2 kΩ]   [100 nF]  │
                   │         │      │
                   └─────────┴──────┘ GND
```

| From | To | Why |
|---|---|---|
| ACS712 VCC | ESP32 **VIN** (5 V) | The ACS712 needs 5 V. VIN carries 5 V when the board is powered from USB or the 5 V adapter. |
| ACS712 GND | ESP32 GND | Common ground |
| ACS712 OUT | 1 kΩ → node A | Top half of the divider |
| node A | 2 kΩ → GND | Bottom half. The output is scaled by 2/3, so the sensor's 0–5 V becomes 0–3.33 V (safe for the ESP32). |
| node A | 100 nF → GND | Low-pass filter (~2.4 kHz) that cuts ADC noise and still passes 50/60 Hz |
| node A | **GPIO34** | ADC1 input. Don't use ADC2 pins, because they stop working when Wi-Fi is on. |

Power the ESP32 from the 5 V 2 A adapter through micro-USB.

## Local indicators (LCD, servo dial, LED)

| Part | Pin | ESP32 | Notes |
|---|---|---|---|
| 16x2 LCD I2C backpack | VCC | VIN (5 V) | LCD needs 5 V for contrast |
| | GND | GND | |
| | SDA | GPIO21 | |
| | SCL | GPIO22 | |
| SG90 servo (analog dial) | red | VIN (5 V) | **Never** from the 3.3 V pin (brown-outs) |
| | brown | GND | |
| | orange (signal) | GPIO18 | |
| Alert LED | – | GPIO2 | The DevKit's on-board blue LED. For an external LED use GPIO2 → 220 Ω → LED → GND. |

- **I2C address:** most backpacks are `0x27`. If the backlight is on but no text appears, try `0x3F`
  (`LCD_I2C_ADDR` in `config.h`), then turn the blue contrast potentiometer on the backpack.
- **Servo and measurement accuracy:** the ACS712 output is *ratiometric*, meaning it scales with its 5 V supply.
  Servo current spikes dip that rail and show up as measurement noise. The firmware only moves the servo
  for changes of 2° or more, between sampling windows. If the readings still jitter when the needle moves,
  add a 470 µF electrolytic across the servo's 5 V/GND at the servo.
- **Dial face:** 0–180° maps to 0–2300 VA (`SERVO_FULL_SCALE_VA`). Print a semicircle scale marked
  0 / 575 / 1150 / 1725 / 2300 VA (0–10 A). If the needle sweeps backwards, set `SERVO_REVERSED true`.
- **The LCD shows:** line 1 is RMS current and apparent power (`I 1.23A S  283VA`); line 2 is the state
  (`IDLE` / `ACTIVE` / `OVERLOAD`) and the link status (`MQTT OK` / `WIFI OK` / `NO NET`).
- **The LED** blinks while current is above `ALERT_CURRENT_A` (8 A default, the facility safety threshold).

## Sensor version

Set `ACS712_MV_PER_AMP` in [firmware/include/config.h](../firmware/include/config.h) to match your module:

| Module | mV/A |
|---|---|
| ACS712-05B | 185 |
| ACS712-20A | 100 (default) |
| ACS712-30A | 66 |

## Calibration

1. With **no load** plugged in, open the serial monitor. `current_a` should read `0`. If idle noise
   shows above zero, raise `NOISE_FLOOR_AMPS`.
2. Plug in a known resistive load (a kettle, heater or incandescent bulb) and compare against a clamp meter,
   or against rated watts ÷ mains voltage.
3. Set `CURRENT_CALIBRATION = real_amps / reported_amps`, then rebuild and upload.

Power is estimated as `MAINS_VOLTAGE × Irms × ASSUMED_POWER_FACTOR` because there's no voltage sensor.
This is accurate for resistive loads (heaters, kettles, bulbs). It overestimates motors, SMPS chargers
and LED drivers, which have a power factor below 1.
