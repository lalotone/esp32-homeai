#include "led_ring.h"

#include "bsp_board.h"
#include "led_strip.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "led_ring";
static led_strip_handle_t s_strip = NULL;

esp_err_t led_ring_init(void)
{
    led_strip_config_t strip_config = {
        .strip_gpio_num = LED_STRIP_GPIO_PIN,
        .max_leds = LED_STRIP_LED_COUNT,
        .led_model = LED_MODEL_WS2812,
        .color_component_format = LED_STRIP_COLOR_COMPONENT_FMT_GRB,
    };
    led_strip_rmt_config_t rmt_config = {
        .resolution_hz = 10 * 1000 * 1000,
    };

    esp_err_t ret = led_strip_new_rmt_device(&strip_config, &rmt_config, &s_strip);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init LED ring: %s", esp_err_to_name(ret));
        return ret;
    }

    return led_strip_clear(s_strip);
}

esp_err_t led_ring_set_color(uint8_t r, uint8_t g, uint8_t b)
{
    if (!s_strip) {
        return ESP_ERR_INVALID_STATE;
    }
    for (int i = 0; i < LED_STRIP_LED_COUNT; i++) {
        led_strip_set_pixel(s_strip, i, r, g, b);
    }
    return led_strip_refresh(s_strip);
}

esp_err_t led_ring_off(void)
{
    if (!s_strip) {
        return ESP_ERR_INVALID_STATE;
    }
    return led_strip_clear(s_strip);
}

esp_err_t led_ring_flash(uint8_t r, uint8_t g, uint8_t b, int times)
{
    for (int i = 0; i < times; i++) {
        led_ring_set_color(r, g, b);
        vTaskDelay(pdMS_TO_TICKS(120));
        led_ring_off();
        vTaskDelay(pdMS_TO_TICKS(120));
    }
    return ESP_OK;
}
