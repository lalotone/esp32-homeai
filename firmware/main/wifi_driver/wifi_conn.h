#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Brings up netif + WiFi station using the saved (or Kconfig default)
// SSID/password, and blocks until connected or a timeout elapses. If the
// timeout is hit, opens the config portal (see config_portal.h) so the
// device can be reconfigured without a reflash, and keeps retrying the STA
// connection in the background. Holding the BOOT button (GPIO0) through
// power-on/reset skips the connect attempt and opens the portal
// immediately, for reconfiguring a board that would otherwise connect fine.
// Call app_config_init() first. Safe to call once at boot; reconnection on
// drops is handled internally afterward.
esp_err_t wifi_conn_init(void);

bool wifi_conn_is_connected(void);

#ifdef __cplusplus
}
#endif
