#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_timer.h"

#include "bsp_board.h"
#include "tca9555_driver.h"
#include "mic_speech.h"
#include "led_ring.h"
#include "audio_echo.h"
#include "wifi_conn.h"
#include "voice_assistant.h"
#include "app_config.h"

static char *TAG = "app main";



static void Speech_event_callback(esp_sr_rec_event_t event,esp_sr_evt_data_t evt_data, void *user_data)
{
    static bool play_flag = 0;
    switch (event)
    {
        case ESP_SR_EVT_AWAKEN:
            ESP_LOGI(TAG, "ESP_SR_EVT_AWAKEN = %d",evt_data.awaken_channel);
            led_ring_set_color(0, 255, 255); // solid cyan: listening for a command
            break;
        case ESP_SR_EVT_CMD:

            switch (evt_data.sr_cmd)
            {
                case 0:
                    ESP_LOGI(TAG, "Turn on the backlight");
                    break;
                case 1:
                    ESP_LOGI(TAG, "Turn off the backlight");
                    break;
                case 2:
                    ESP_LOGI(TAG, "Backlight is brightest");
                    break;
                case 3:
                    ESP_LOGI(TAG, "Backlight is darkest");
                    break;
                case 4:
                    ESP_LOGI(TAG, "Recording for five seconds");
                    audio_echo_record_and_play(5);
                    break;
                case 5:
                    ESP_LOGI(TAG, "Volume: highest");
                    esp_audio_set_play_vol(100);
                    break;
                case 6:
                    ESP_LOGI(TAG, "Volume: lowest");
                    esp_audio_set_play_vol(0);
                    break;
                case 7: {
                    int vol = 0;
                    esp_audio_get_play_vol(&vol);
                    vol = (vol + 10 > 100) ? 100 : vol + 10;
                    esp_audio_set_play_vol(vol);
                    ESP_LOGI(TAG, "Volume: up to %d", vol);
                    break;
                }
                case 8: {
                    int vol = 0;
                    esp_audio_get_play_vol(&vol);
                    vol = (vol - 10 < 0) ? 0 : vol - 10;
                    esp_audio_set_play_vol(vol);
                    ESP_LOGI(TAG, "Volume: down to %d", vol);
                    break;
                }
                case 9:
                    if (wifi_conn_is_connected()) {
                        ESP_LOGI(TAG, "Assistant: recording and asking");
                        voice_assistant_ask(); // records, uploads, and plays back the response; manages its own LED staging
                    } else {
                        ESP_LOGW(TAG, "Assistant command heard, but WiFi not connected");
                        led_ring_flash(255, 0, 0, 2); // red flash: assistant unavailable
                    }
                    break;
                default:
                    break;
            }
            led_ring_flash(0, 255, 0, 1);        // quick green flash: command recognized
            led_ring_set_color(0, 255, 255);     // back to solid cyan: still listening
            break;
        case ESP_SR_EVT_CMD_TIMEOUT:
            ESP_LOGI(TAG, "ESP_SR_EVT_CMD_TIMEOUT");
            led_ring_off(); // back to idle: waiting for the wake word again
            break;
        default:
            break;
    }
}


void app_main()
{
    ESP_ERROR_CHECK(esp_board_init(16000, 2, 16));
    tca9555_driver_init();
    ESP_ERROR_CHECK(led_ring_init());
    led_ring_off(); // idle: waiting for the wake word

    ESP_ERROR_CHECK(app_config_init());
    if (wifi_conn_init() != ESP_OK) {
        ESP_LOGW(TAG, "Starting without WiFi connected; voice assistant requests will be skipped until it connects "
                       "(or reconfigured via the config portal AP)");
    }

    Speech_Init();
    Speech_register_callback(Speech_event_callback);
}

