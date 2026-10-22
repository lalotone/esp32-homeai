#include "button_driver.h"
#include "tca9555_driver.h"

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "button_driver";

#define KEY_SCAN_MS   100   // poll rate; far above mechanical debounce
#define LONG_PRESS_MS 1000

static const uint32_t KEY_PIN_MASK =
    (1ULL << KEY_ID_9) | (1ULL << KEY_ID_10) | (1ULL << KEY_ID_11);
static const key_id_t KEY_IDS[3] = { KEY_ID_9, KEY_ID_10, KEY_ID_11 };

typedef struct {
    bool pressed;         // current debounced state (true == pressed)
    bool last_pressed;
    int64_t press_start_ms;
    bool long_fired;
} key_state_t;

static key_state_t s_keys[3];
static key_callback_t s_callback = NULL;
static void *s_user_data = NULL;

// Active-low: a pressed key reads 0 on its bit.
static bool key_is_pressed(uint32_t mask, key_id_t id)
{
    return !(mask & (1ULL << id));
}

static void button_scan_task(void *arg)
{
    while (1) {
        uint32_t mask = 0;
        // One I2C read grabs all three pins at once.
        if (esp_io_expander_get_level(io_expander, KEY_PIN_MASK, &mask) != ESP_OK) {
            vTaskDelay(pdMS_TO_TICKS(KEY_SCAN_MS));
            continue;
        }

        int64_t now = esp_timer_get_time() / 1000; // ms

        for (int i = 0; i < 3; i++) {
            key_id_t id = KEY_IDS[i];
            key_state_t *k = &s_keys[i];
            bool pressed = key_is_pressed(mask, id);

            if (pressed && !k->last_pressed) {
                k->press_start_ms = now;
                k->long_fired = false;
            } else if (!pressed && k->last_pressed) {
                // Released: it was a short press unless a long press already fired.
                int32_t held = (int32_t)(now - k->press_start_ms);
                if (!k->long_fired && held < LONG_PRESS_MS && s_callback) {
                    s_callback(id, KEY_EVENT_SHORT_PRESS, s_user_data);
                }
            } else if (pressed && !k->long_fired && now - k->press_start_ms >= LONG_PRESS_MS) {
                if (s_callback) {
                    s_callback(id, KEY_EVENT_LONG_PRESS, s_user_data);
                }
                k->long_fired = true; // fire the long press exactly once
            }

            k->pressed = pressed;
            k->last_pressed = pressed;
        }

        vTaskDelay(pdMS_TO_TICKS(KEY_SCAN_MS));
    }
}

esp_err_t key_module_init(void *user_data)
{
    s_user_data = user_data;
    for (int i = 0; i < 3; i++) {
        s_keys[i].pressed = false;
        s_keys[i].last_pressed = false;
        s_keys[i].press_start_ms = 0;
        s_keys[i].long_fired = false;
    }
    if (xTaskCreatePinnedToCore(button_scan_task, "btn_scan", 4096, NULL, 3, NULL, 0) != pdPASS) {
        ESP_LOGE(TAG, "Failed to start button scan task");
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t key_register_callback(key_callback_t callback)
{
    if (!callback) {
        return ESP_ERR_INVALID_ARG;
    }
    s_callback = callback;
    return ESP_OK;
}
