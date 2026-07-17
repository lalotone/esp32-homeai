#include "wifi_conn.h"
#include "app_config.h"
#include "config_portal.h"

#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/event_groups.h"
#include <string.h>

static const char *TAG = "wifi_conn";

#define WIFI_CONNECTED_BIT      BIT0
#define WIFI_STARTUP_RETRY_LOG  5
#define WIFI_CONNECT_TIMEOUT_MS 15000

// The BOOT button (GPIO0, active-low) isn't used by any app code once
// flashing is done, so it doubles as a "force the config portal open"
// switch: hold it through power-on/reset to skip straight to setup mode
// instead of trying the saved WiFi credentials first.
#define FORCE_PORTAL_GPIO GPIO_NUM_0

static bool force_portal_requested(void)
{
    gpio_config_t io_conf = {
        .pin_bit_mask = 1ULL << FORCE_PORTAL_GPIO,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
    vTaskDelay(pdMS_TO_TICKS(10)); // let the pull-up settle before sampling
    return gpio_get_level(FORCE_PORTAL_GPIO) == 0;
}

static EventGroupHandle_t s_wifi_event_group;
static int s_retry_num = 0;
static volatile bool s_connected = false;
static volatile bool s_portal_active = false;

// Retries forever on disconnect (not just during the initial connect
// window), since this is a long-running device that should keep trying to
// get back online after e.g. the router reboots. The one exception is while
// the config portal is up: each STA (re)connect attempt triggers a channel
// scan, which knocks the softAP off the air for a moment on this
// single-radio chip and makes the portal page flaky/unreachable. Since
// saving new config through the portal reboots the board anyway, it's not
// worth fighting for STA reconnection in the background during that window.
static void event_handler(void *arg, esp_event_base_t event_base, int32_t event_id, void *event_data)
{
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        s_connected = false;
        s_retry_num++;
        if (s_retry_num <= WIFI_STARTUP_RETRY_LOG) {
            ESP_LOGW(TAG, "WiFi disconnected, retrying (%d)...", s_retry_num);
        }
        if (!s_portal_active) {
            esp_wifi_connect();
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *) event_data;
        ESP_LOGI(TAG, "Connected, IP: " IPSTR, IP2STR(&event->ip_info.ip));
        s_retry_num = 0;
        s_connected = true;
        xEventGroupSetBits(s_wifi_event_group, WIFI_CONNECTED_BIT);

        if (s_portal_active) {
            s_portal_active = false;
            config_portal_drop_ap(); // fallback AP no longer needed; config server stays up on the station IP
        }
        config_portal_start_server(); // always reachable while on WiFi, even if the fallback AP was never needed
    }
}

bool wifi_conn_is_connected(void)
{
    return s_connected;
}

esp_err_t wifi_conn_init(void)
{
    s_wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();
    esp_netif_create_default_wifi_ap(); // idle until config_portal_start() configures + enables it

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &event_handler, NULL));

    char ssid[APP_CONFIG_SSID_MAX_LEN];
    char pass[APP_CONFIG_PASS_MAX_LEN];
    app_config_get_wifi(ssid, sizeof(ssid), pass, sizeof(pass));

    wifi_config_t wifi_config = { 0 };
    strlcpy((char *)wifi_config.sta.ssid, ssid, sizeof(wifi_config.sta.ssid));
    strlcpy((char *)wifi_config.sta.password, pass, sizeof(wifi_config.sta.password));
    wifi_config.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    bool forced = force_portal_requested();
    EventBits_t bits = 0;
    if (forced) {
        ESP_LOGW(TAG, "BOOT button held at startup; skipping straight to config portal");
    } else {
        ESP_LOGI(TAG, "Connecting to SSID '%s'...", ssid);
        bits = xEventGroupWaitBits(s_wifi_event_group, WIFI_CONNECTED_BIT,
                                    pdFALSE, pdFALSE, pdMS_TO_TICKS(WIFI_CONNECT_TIMEOUT_MS));
    }

    if (bits & WIFI_CONNECTED_BIT) {
        return ESP_OK;
    }

    if (!forced) {
        ESP_LOGW(TAG, "Failed to connect to WiFi within %d ms; opening config portal while retrying in the background", WIFI_CONNECT_TIMEOUT_MS);
    }
    if (config_portal_start_ap() == ESP_OK) {
        s_portal_active = true;
    }
    return ESP_FAIL;
}
