#include "audio_echo.h"

#include "bsp_board.h"
#include "mic_speech.h"
#include "led_ring.h"

#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio_echo";

#define RECORD_CHUNK_FRAMES 512
// Position of a real microphone sample within each captured frame, per the
// board's fixed "RMNM" channel layout (R=AEC reference, M=mic, N=unused).
#define MIC_CHANNEL_INDEX   1

// VAD capture tuning (see audio_capture_vad). Thresholds are in int16 sample
// units; log the measured RMS while tuning so these can be dialed to the room.
#define VAD_MAX_SECONDS     10   // hard cap on one capture (a user who never speaks gets 10s of silence -> API gate)
#define VAD_SILENCE_MS      1000 // end capture once this much low-RMS silence has followed speech;
                                 // must be long enough to swallow a natural thinking-pause (~0.5-1s)
                                 // so it isn't mistaken for the end of the question. 1000ms was chosen
                                 // because 500ms cut real pauses out of the middle of questions.
// Dialed to this room from the "VAD: rms_sq=" log lines: noise floor rms_sq ~50-250,
// normal speech rms_sq ~15000+ (rms ~120-160). 60 (rms_sq 3600) sits well above
// the floor and below speech; re-tune if the mic or room changes.
#define VAD_SILENCE_RMS     60

esp_err_t audio_capture_mono(int seconds, int16_t **out_buf, int *out_frames)
{
    if (seconds <= 0 || !out_buf || !out_frames) {
        return ESP_ERR_INVALID_ARG;
    }

    int feed_channels = esp_get_feed_channel();
    int total_frames = AUDIO_CAPTURE_SAMPLE_RATE * seconds;

    int16_t *chunk_buf = heap_caps_malloc((size_t)RECORD_CHUNK_FRAMES * feed_channels * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    int16_t *mono_buf = heap_caps_malloc((size_t)total_frames * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!chunk_buf || !mono_buf) {
        ESP_LOGE(TAG, "Failed to allocate %d seconds of recording buffer", seconds);
        free(chunk_buf);
        free(mono_buf);
        return ESP_ERR_NO_MEM;
    }

    // feed_Task also reads the mic continuously, and its AEC/beamforming/WakeNet
    // processing competes for the same I2S peripheral used for playback -- so we
    // pause it here and leave it paused; the caller is responsible for resuming
    // it once any subsequent playback is also done (see mic_speech_pause_feed
    // callers in audio_echo_record_and_play() and voice_assistant_ask()).
    mic_speech_pause_feed(true);
    ESP_LOGI(TAG, "Recording %d second(s)...", seconds);

    int frames_captured = 0;
    while (frames_captured < total_frames) {
        int frames_this_chunk = RECORD_CHUNK_FRAMES;
        if (frames_captured + frames_this_chunk > total_frames) {
            frames_this_chunk = total_frames - frames_captured;
        }

        esp_get_feed_data(true, chunk_buf, frames_this_chunk * feed_channels * sizeof(int16_t));

        for (int i = 0; i < frames_this_chunk; i++) {
            mono_buf[frames_captured + i] = chunk_buf[i * feed_channels + MIC_CHANNEL_INDEX];
        }
        frames_captured += frames_this_chunk;
    }
    free(chunk_buf);

    *out_buf = mono_buf;
    *out_frames = total_frames;
    return ESP_OK;
}

esp_err_t audio_capture_vad(int16_t **out_buf, int *out_frames)
{
    bool heard;
    return audio_capture_vad_wait(0, 1, out_buf, out_frames, &heard);
}

esp_err_t audio_capture_vad_wait(int start_timeout_ms, int min_speech_chunks, int16_t **out_buf, int *out_frames,
                                 bool *out_heard)
{
    if (!out_buf || !out_frames || !out_heard || min_speech_chunks < 1) {
        return ESP_ERR_INVALID_ARG;
    }
    *out_heard = false;
    int start_timeout_frames = AUDIO_CAPTURE_SAMPLE_RATE / 1000 * start_timeout_ms;

    int feed_channels = esp_get_feed_channel();
    int max_frames = AUDIO_CAPTURE_SAMPLE_RATE * VAD_MAX_SECONDS;
    int silence_frames = AUDIO_CAPTURE_SAMPLE_RATE * VAD_SILENCE_MS / 1000;
    // Compare mean square against the threshold squared instead of taking a
    // sqrt per chunk: integer-only, no FPU work in the hot loop.
    int64_t silence_rms_sq = (int64_t)VAD_SILENCE_RMS * (int64_t)VAD_SILENCE_RMS;

    int16_t *chunk_buf = heap_caps_malloc((size_t)RECORD_CHUNK_FRAMES * feed_channels * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    int16_t *mono_buf = heap_caps_malloc((size_t)max_frames * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!chunk_buf || !mono_buf) {
        ESP_LOGE(TAG, "Failed to allocate VAD capture buffer");
        free(chunk_buf);
        free(mono_buf);
        return ESP_ERR_NO_MEM;
    }

    // Same feed_Task pause as audio_capture_mono (see that comment for why);
    // the caller is responsible for resuming it.
    mic_speech_pause_feed(true);
    ESP_LOGI(TAG, "VAD capture (max %ds)...", VAD_MAX_SECONDS);

    int frames_captured = 0;
    int silence_run = 0;      // consecutive low-RMS samples once speech has started
    int loud_run = 0;         // consecutive loud chunks before speech counts as started
    bool started = false;     // have we seen the user actually start talking?
    int log_next = AUDIO_CAPTURE_SAMPLE_RATE / 2; // log measured RMS ~every 500ms while tuning

    while (frames_captured < max_frames) {
        esp_get_feed_data(true, chunk_buf, RECORD_CHUNK_FRAMES * feed_channels * sizeof(int16_t));

        int64_t sumsq = 0;
        for (int i = 0; i < RECORD_CHUNK_FRAMES; i++) {
            int16_t s = chunk_buf[i * feed_channels + MIC_CHANNEL_INDEX];
            sumsq += (int64_t)s * (int64_t)s;
        }
        int64_t rms_sq = sumsq / RECORD_CHUNK_FRAMES;

        if (rms_sq >= silence_rms_sq) {
            silence_run = 0;
            if (++loud_run >= min_speech_chunks) {
                started = true;      // speech (or loud noise) present long enough
            }
        } else {
            loud_run = 0;
            if (started) {
                silence_run += RECORD_CHUNK_FRAMES;  // trailing silence after speech
            }
        }

        for (int i = 0; i < RECORD_CHUNK_FRAMES; i++) {
            mono_buf[frames_captured + i] = chunk_buf[i * feed_channels + MIC_CHANNEL_INDEX];
        }
        frames_captured += RECORD_CHUNK_FRAMES;

        if (frames_captured >= log_next) {
            ESP_LOGI(TAG, "VAD: %dms rms_sq=%lld started=%d",
                     frames_captured * 1000 / AUDIO_CAPTURE_SAMPLE_RATE,
                     (long long)rms_sq, (int)started);
            log_next += AUDIO_CAPTURE_SAMPLE_RATE / 2;
        }

        // Stop once the user has spoken and then stopped for long enough.
        // Leading silence before the first word is deliberately captured (the
        // API's own gate handles a recording that never contains speech).
        if (started && silence_run >= silence_frames) {
            ESP_LOGI(TAG, "VAD: end of speech after %dms",
                     frames_captured * 1000 / AUDIO_CAPTURE_SAMPLE_RATE);
            break;
        }
        // Follow-up window: nobody started talking in time, so give up early.
        if (!started && start_timeout_frames > 0 && frames_captured >= start_timeout_frames) {
            break;
        }
    }
    *out_heard = started;

    if (!started) {
        ESP_LOGI(TAG, "VAD: no speech detected (%dms of silence captured)",
                 frames_captured * 1000 / AUDIO_CAPTURE_SAMPLE_RATE);
    }

    free(chunk_buf);
    *out_buf = mono_buf;
    *out_frames = frames_captured;
    return ESP_OK;
}

esp_err_t audio_echo_record_and_play(int seconds)
{
    int16_t *mono_buf = NULL;
    int total_frames = 0;

    led_ring_set_color(255, 0, 255); // magenta: recording
    esp_err_t ret = audio_capture_mono(seconds, &mono_buf, &total_frames);
    if (ret != ESP_OK) {
        led_ring_off();
        return ret; // feed_Task was never paused if capture itself failed
    }

    // Stereo, since the DAC is opened as a 2-channel device (esp_board_init(.., 2, ..)).
    int16_t *stereo_buf = heap_caps_malloc((size_t)total_frames * 2 * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!stereo_buf) {
        ESP_LOGE(TAG, "Failed to allocate playback buffer");
        free(mono_buf);
        mic_speech_pause_feed(false);
        led_ring_off();
        return ESP_ERR_NO_MEM;
    }
    for (int i = 0; i < total_frames; i++) {
        stereo_buf[i * 2 + 0] = mono_buf[i]; // L
        stereo_buf[i * 2 + 1] = mono_buf[i]; // R
    }
    free(mono_buf);

    ESP_LOGI(TAG, "Playing back...");
    led_ring_set_color(0, 128, 255); // blue: playing back
    esp_audio_play(stereo_buf, total_frames * 2 * sizeof(int16_t), portMAX_DELAY);
    free(stereo_buf);

    // feed_Task stayed paused since audio_capture_mono() so playback above
    // doesn't have to compete with it for the shared I2S peripheral -- safe
    // to hand the mic back now that we're fully done with it.
    mic_speech_pause_feed(false);

    led_ring_off();
    ESP_LOGI(TAG, "Done");
    return ESP_OK;
}
