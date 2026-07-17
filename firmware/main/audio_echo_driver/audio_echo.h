#pragma once

#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AUDIO_CAPTURE_SAMPLE_RATE 16000

// Records `seconds` of mono 16-bit PCM audio at AUDIO_CAPTURE_SAMPLE_RATE
// from the mic array into a buffer allocated in PSRAM (caller must free it).
// Pauses feed_Task (see mic_speech_pause_feed) for the duration, since it's
// the only other reader of the mic.
esp_err_t audio_capture_mono(int seconds, int16_t **out_buf, int *out_frames);

// Records a few seconds of audio from the mic array and plays it back
// on the speaker. Blocks the calling task for the duration of both.
esp_err_t audio_echo_record_and_play(int seconds);

#ifdef __cplusplus
}
#endif
