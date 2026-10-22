#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Records audio, POSTs it to CONFIG_VOICE_ASSISTANT_ENDPOINT_URL, and plays
// back whatever comes back on the speaker. Blocks the calling task for the
// whole round trip (recording + upload + response playback).
esp_err_t voice_assistant_ask(void);

// Starts the background poll of the API's /pending endpoint: plays timer and
// reminder announcements when they're due, never during a voice_assistant_ask().
esp_err_t voice_assistant_start_announcements(void);

#ifdef __cplusplus
}
#endif
