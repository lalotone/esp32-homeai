#pragma once

#include <stddef.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define APP_CONFIG_SSID_MAX_LEN 32
#define APP_CONFIG_PASS_MAX_LEN 64
#define APP_CONFIG_URL_MAX_LEN  128

// Initializes NVS and loads any saved WiFi credentials / assistant URL into
// RAM. Must be called once at boot, before wifi_conn_init() or any getter
// below.
esp_err_t app_config_init(void);

// Fills ssid/pass with the saved WiFi credentials, or the Kconfig defaults
// (CONFIG_ESP_WIFI_SSID/PASSWORD) if nothing has been saved via the config
// portal yet.
void app_config_get_wifi(char *ssid, size_t ssid_size, char *pass, size_t pass_size);

// Persists new WiFi credentials to NVS; takes effect on next connect attempt
// / after reboot.
esp_err_t app_config_set_wifi(const char *ssid, const char *pass);

// Fills url with the saved voice-assistant endpoint, or the Kconfig default
// (CONFIG_VOICE_ASSISTANT_ENDPOINT_URL) if nothing has been saved yet.
void app_config_get_llm_url(char *url, size_t url_size);

esp_err_t app_config_set_llm_url(const char *url);

#ifdef __cplusplus
}
#endif
