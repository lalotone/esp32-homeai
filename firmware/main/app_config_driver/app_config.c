#include "app_config.h"

#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "app_config";
#define NVS_NAMESPACE "app_cfg"

// Seeded from Kconfig so a freshly flashed board behaves exactly as before;
// overwritten from NVS in app_config_init() if the config portal has ever
// saved something.
static char s_ssid[APP_CONFIG_SSID_MAX_LEN] = CONFIG_ESP_WIFI_SSID;
static char s_pass[APP_CONFIG_PASS_MAX_LEN] = CONFIG_ESP_WIFI_PASSWORD;
static char s_url[APP_CONFIG_URL_MAX_LEN]   = CONFIG_VOICE_ASSISTANT_ENDPOINT_URL;

static void load_string(nvs_handle_t h, const char *key, char *out, size_t out_size)
{
    size_t len = out_size;
    nvs_get_str(h, key, out, &len); // leaves `out` untouched (Kconfig default) if key is absent
}

esp_err_t app_config_init(void)
{
    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);

    nvs_handle_t h;
    if (nvs_open(NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) {
        ESP_LOGI(TAG, "No saved config yet, using Kconfig defaults");
        return ESP_OK;
    }

    load_string(h, "ssid", s_ssid, sizeof(s_ssid));
    load_string(h, "pass", s_pass, sizeof(s_pass));
    load_string(h, "url", s_url, sizeof(s_url));
    nvs_close(h);

    ESP_LOGI(TAG, "Loaded saved config: SSID '%s', assistant URL '%s'", s_ssid, s_url);
    return ESP_OK;
}

void app_config_get_wifi(char *ssid, size_t ssid_size, char *pass, size_t pass_size)
{
    strlcpy(ssid, s_ssid, ssid_size);
    strlcpy(pass, s_pass, pass_size);
}

esp_err_t app_config_set_wifi(const char *ssid, const char *pass)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_str(h, "ssid", ssid);
    if (err == ESP_OK) {
        err = nvs_set_str(h, "pass", pass);
    }
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);

    if (err == ESP_OK) {
        strlcpy(s_ssid, ssid, sizeof(s_ssid));
        strlcpy(s_pass, pass, sizeof(s_pass));
    }
    return err;
}

void app_config_get_llm_url(char *url, size_t url_size)
{
    strlcpy(url, s_url, url_size);
}

esp_err_t app_config_set_llm_url(const char *url)
{
    nvs_handle_t h;
    esp_err_t err = nvs_open(NVS_NAMESPACE, NVS_READWRITE, &h);
    if (err != ESP_OK) {
        return err;
    }

    err = nvs_set_str(h, "url", url);
    if (err == ESP_OK) {
        err = nvs_commit(h);
    }
    nvs_close(h);

    if (err == ESP_OK) {
        strlcpy(s_url, url, sizeof(s_url));
    }
    return err;
}
