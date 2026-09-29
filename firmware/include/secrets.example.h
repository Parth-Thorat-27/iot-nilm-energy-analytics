#pragma once
// Copy this file to secrets.h and fill in your values. secrets.h is git-ignored.

#define WIFI_SSID "YOUR_WIFI_SSID"          // ESP32 supports 2.4 GHz networks only
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"

#define MQTT_HOST "192.168.1.100"           // LAN IP of the PC running Docker
#define MQTT_PORT 1883
#define MQTT_USER ""                        // leave empty for anonymous broker
#define MQTT_PASSWORD ""

#define SITE_ID "hostel"                    // topic: hostel/room101/power
#define ROOM_ID "room101"
#define DEVICE_ID "esp32-room101"           // unique per board
