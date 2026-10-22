#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// The buttons sit on the TCA9555 io expander. tca9555_driver_init() already
// configures pins 9/10/11 as inputs; these are active-low (a press reads 0).
typedef enum {
    KEY_ID_9  = 9,   // K1
    KEY_ID_10 = 10,  // K2 / SET
    KEY_ID_11 = 11   // K3 / -
} key_id_t;

typedef enum {
    KEY_EVENT_SHORT_PRESS, // released before LONG_PRESS_MS
    KEY_EVENT_LONG_PRESS   // held for at least LONG_PRESS_MS
} key_event_t;

typedef void (*key_callback_t)(key_id_t key_id, key_event_t event, void *user_data);

// Starts the debounced button scan task. Must be called after
// tca9555_driver_init() (the pins must already be inputs).
esp_err_t key_module_init(void *user_data);

// Registers the callback fired on each (de-bounced) press. Call before key_module_init().
esp_err_t key_register_callback(key_callback_t callback);

#ifdef __cplusplus
}
#endif
