#pragma once

#include <stdbool.h>
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

// VAD (voice-activity) capture: records from the mic until the user stops
// talking (sustained low-RMS silence) or a maximum duration elapses, whichever
// comes first. Unlike audio_capture_mono() the output length is variable, given
// back in *out_frames. Always returns some audio -- even pure silence -- so the
// API stays the single place that decides a recording is "nothing said".
// Pauses feed_Task for the duration (see audio_capture_mono); the caller resumes it.
esp_err_t audio_capture_vad(int16_t **out_buf, int *out_frames);

// Same, for a follow-up question: gives up after `start_timeout_ms` if nobody
// starts talking (0 = wait the whole VAD_MAX_SECONDS), and only counts speech
// as started after `min_speech_chunks` consecutive loud 32 ms chunks, so a
// click or the tail of the speaker's own reply doesn't open a turn.
// *out_heard says whether speech started; the buffer is returned either way.
esp_err_t audio_capture_vad_wait(int start_timeout_ms, int min_speech_chunks, int16_t **out_buf, int *out_frames,
                                 bool *out_heard);

// Records a few seconds of audio from the mic array and plays it back
// on the speaker. Blocks the calling task for the duration of both.
esp_err_t audio_echo_record_and_play(int seconds);

#ifdef __cplusplus
}
#endif
