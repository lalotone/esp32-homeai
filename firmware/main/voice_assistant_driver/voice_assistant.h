#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Records audio, POSTs it to CONFIG_VOICE_ASSISTANT_ENDPOINT_URL, and plays
// back whatever comes back on the speaker. Blocks the calling task for the
// whole round trip (recording + upload + response playback).
esp_err_t voice_assistant_ask(void);

#ifdef __cplusplus
}
#endif
