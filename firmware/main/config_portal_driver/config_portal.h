#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Starts the HTTP config server (idempotent) with a form for WiFi
// SSID/password and the assistant server URL, at whatever IP(s) the board
// currently has -- doesn't touch WiFi mode itself, so it's reachable at the
// AP IP (192.168.4.1) and/or the station's own IP depending on what's up.
// Saving the form persists the values via app_config and reboots the board.
esp_err_t config_portal_start_server(void);

// Switches WiFi to APSTA, brings up an open access point named
// "<CONFIG_PORTAL_AP_SSID_PREFIX>-XXXX", and starts the config server (see
// above) at http://192.168.4.1/. Used when STA can't connect and the board
// needs to be reconfigured from scratch.
esp_err_t config_portal_start_ap(void);

// Drops back to WIFI_MODE_STA once the station has connected, tearing down
// the fallback AP. Leaves the HTTP server running -- it's reachable at the
// station's own IP from then on, so the LLM URL (or WiFi creds) can still
// be changed live without ever re-opening the AP.
void config_portal_drop_ap(void);

#ifdef __cplusplus
}
#endif
