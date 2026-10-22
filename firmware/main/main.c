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
#include "button_driver.h"

static char *TAG = "app main";


// Physical volume buttons (on the TCA9555 io expander):
//   K2 / SET (KEY_ID_10) -> volume up,  K3 / - (KEY_ID_11) -> volume down.
// Runs in the button scan task. Short press only; long press is ignored.
static void button_callback(key_id_t key_id, key_event_t event, void *user_data)
{
    (void)user_data;
    if (event != KEY_EVENT_SHORT_PRESS) {
        return;
    }

    int vol = 0;
    esp_audio_get_play_vol(&vol);
    if (key_id == KEY_ID_10) {        // K2 / SET: up
        vol = (vol + 10 > 100) ? 100 : vol + 10;
        led_ring_flash(0, 255, 0, 1); // green flash: volume up
    } else if (key_id == KEY_ID_11) { // K3 / - : down
        vol = (vol - 10 < 0) ? 0 : vol - 10;
        led_ring_flash(255, 128, 0, 1); // orange flash: volume down
    } else {
        return; // K1 (KEY_ID_9) is not wired to a function
    }
    esp_audio_set_play_vol(vol);
    ESP_LOGI(TAG, "Button %d: volume -> %d", (int)key_id, vol);
}


static void Speech_event_callback(esp_sr_rec_event_t event, esp_sr_evt_data_t evt_data, void *user_data)
{
    (void)user_data;
    switch (event)
    {
        case ESP_SR_EVT_AWAKEN:
            // Single wake word ("Alexa") now goes straight to the assistant:
            // VAD-capture the question, upload, and play the spoken reply.
            ESP_LOGI(TAG, "Woken by wake word (channel %d)", evt_data.awaken_channel);
            if (wifi_conn_is_connected()) {
                voice_assistant_ask(); // VAD-captures, uploads, and plays back the response; manages its own LED staging
            } else {
                ESP_LOGW(TAG, "Woken, but WiFi not connected");
                led_ring_flash(255, 0, 0, 2); // red flash: assistant unavailable
                led_ring_off();
            }
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

    key_register_callback(button_callback);
    key_module_init(NULL); // start the debounced button scan (volume up/down)

    Speech_Init();
    Speech_register_callback(Speech_event_callback);

    if (voice_assistant_start_announcements() != ESP_OK) {
        ESP_LOGW(TAG, "Timer/reminder announcements unavailable");
    }
}

