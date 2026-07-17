#include "config_portal.h"
#include "app_config.h"

#include "esp_wifi.h"
#include "esp_http_server.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "config_portal";
static httpd_handle_t s_server = NULL;

static const char *FORM_HTML =
    "<!DOCTYPE html><html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
    "<title>Board Setup</title></head>"
    "<body style='font-family:sans-serif;max-width:420px;margin:2em auto;padding:0 1em'>"
    "<h2>Board Setup</h2>"
    "<form method='POST' action='/save'>"
    "<label>WiFi SSID</label><br><input name='ssid' value='%s' maxlength='31' style='width:100%%'><br><br>"
    "<label>WiFi Password</label><br><input name='pass' type='password' maxlength='63' style='width:100%%'><br><br>"
    "<label>Assistant server URL</label><br><input name='url' value='%s' maxlength='127' style='width:100%%'><br><br>"
    "<button type='submit'>Save &amp; Reboot</button>"
    "</form></body></html>";

static esp_err_t root_get_handler(httpd_req_t *req)
{
    char ssid[APP_CONFIG_SSID_MAX_LEN];
    char pass[APP_CONFIG_PASS_MAX_LEN];
    char url[APP_CONFIG_URL_MAX_LEN];
    app_config_get_wifi(ssid, sizeof(ssid), pass, sizeof(pass));
    app_config_get_llm_url(url, sizeof(url));
    (void)pass; // never pre-filled in the form

    char resp[800 + APP_CONFIG_SSID_MAX_LEN + APP_CONFIG_URL_MAX_LEN]; // FORM_HTML template plus the two substituted values
    int len = snprintf(resp, sizeof(resp), FORM_HTML, ssid, url);
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, resp, len);
    return ESP_OK;
}

// Decodes application/x-www-form-urlencoded values in place ("+" -> space,
// "%XX" -> byte). httpd_query_key_value() only splits on & and =, it
// doesn't decode.
static void url_decode(char *s)
{
    char *o = s;
    while (*s) {
        if (*s == '+') {
            *o++ = ' ';
            s++;
        } else if (*s == '%' && s[1] && s[2]) {
            char hex[3] = { s[1], s[2], 0 };
            *o++ = (char)strtol(hex, NULL, 16);
            s += 3;
        } else {
            *o++ = *s++;
        }
    }
    *o = '\0';
}

static void restart_task(void *arg)
{
    vTaskDelay(pdMS_TO_TICKS(1500)); // let the HTTP response flush before reset
    esp_restart();
}

static esp_err_t save_post_handler(httpd_req_t *req)
{
    char buf[256];
    size_t total = req->content_len < sizeof(buf) - 1 ? req->content_len : sizeof(buf) - 1;
    size_t received = 0;
    while (received < total) {
        int r = httpd_req_recv(req, buf + received, total - received);
        if (r <= 0) {
            httpd_resp_send_500(req);
            return ESP_FAIL;
        }
        received += (size_t)r;
    }
    buf[received] = '\0';

    char ssid[APP_CONFIG_SSID_MAX_LEN] = { 0 };
    char pass[APP_CONFIG_PASS_MAX_LEN] = { 0 };
    char url[APP_CONFIG_URL_MAX_LEN] = { 0 };
    httpd_query_key_value(buf, "ssid", ssid, sizeof(ssid));
    httpd_query_key_value(buf, "pass", pass, sizeof(pass));
    httpd_query_key_value(buf, "url", url, sizeof(url));
    url_decode(ssid);
    url_decode(pass);
    url_decode(url);

    if (strlen(ssid) == 0) {
        httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "SSID required");
        return ESP_FAIL;
    }

    app_config_set_wifi(ssid, pass);
    if (strlen(url) > 0) {
        app_config_set_llm_url(url);
    }
    ESP_LOGI(TAG, "Saved new config (SSID '%s'); rebooting", ssid);

    const char *resp = "<html><body><h3>Saved. Rebooting...</h3></body></html>";
    httpd_resp_set_type(req, "text/html");
    httpd_resp_send(req, resp, strlen(resp));

    xTaskCreate(restart_task, "cfg_restart", 2048, NULL, 5, NULL);
    return ESP_OK;
}

esp_err_t config_portal_start_server(void)
{
    if (s_server) {
        return ESP_OK;
    }

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    config.lru_purge_enable = true;
    esp_err_t err = httpd_start(&s_server, &config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to start config HTTP server: %s", esp_err_to_name(err));
        s_server = NULL;
        return err;
    }

    httpd_uri_t root_uri = { .uri = "/", .method = HTTP_GET, .handler = root_get_handler };
    httpd_uri_t save_uri = { .uri = "/save", .method = HTTP_POST, .handler = save_post_handler };
    httpd_register_uri_handler(s_server, &root_uri);
    httpd_register_uri_handler(s_server, &save_uri);

    ESP_LOGI(TAG, "Config server up on port 80");
    return ESP_OK;
}

esp_err_t config_portal_start_ap(void)
{
    uint8_t mac[6];
    esp_wifi_get_mac(WIFI_IF_AP, mac);
    char ap_ssid[32];
    snprintf(ap_ssid, sizeof(ap_ssid), "%s-%02X%02X", CONFIG_PORTAL_AP_SSID_PREFIX, mac[4], mac[5]);

    wifi_config_t ap_config = { 0 };
    strlcpy((char *)ap_config.ap.ssid, ap_ssid, sizeof(ap_config.ap.ssid));
    ap_config.ap.ssid_len = strlen(ap_ssid);
    ap_config.ap.channel = 1;
    ap_config.ap.max_connection = 4;
    ap_config.ap.authmode = WIFI_AUTH_OPEN;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));

    ESP_LOGW(TAG, "Config portal AP up: join WiFi '%s' (open) and browse to http://192.168.4.1/", ap_ssid);
    return config_portal_start_server();
}

void config_portal_drop_ap(void)
{
    esp_wifi_set_mode(WIFI_MODE_STA);
    ESP_LOGI(TAG, "Dropped fallback AP (WiFi connected); config server stays up on the station IP");
}
