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
