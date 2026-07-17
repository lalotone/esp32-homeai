#pragma once

#include "esp_err.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

esp_err_t led_ring_init(void);

esp_err_t led_ring_set_color(uint8_t r, uint8_t g, uint8_t b);
esp_err_t led_ring_off(void);

// Briefly flashes the given color a few times, then turns the ring off.
esp_err_t led_ring_flash(uint8_t r, uint8_t g, uint8_t b, int times);

#ifdef __cplusplus
}
#endif
