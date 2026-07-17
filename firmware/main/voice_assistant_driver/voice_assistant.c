#include "voice_assistant.h"

#include "bsp_board.h"
#include "audio_echo.h"
#include "led_ring.h"
#include "wifi_conn.h"
#include "mic_speech.h"
#include "app_config.h"

#include "esp_http_client.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"

#include <string.h>

static const char *TAG = "voice_assistant";

#define RECORD_SECONDS      5
// A real server does STT -> LLM -> TTS as three sequential blocking calls
// before it can respond at all, which can easily take longer than a plain
// API round trip -- give it real headroom rather than timing out mid-chain.
#define HTTP_TIMEOUT_MS      60000
// Response is played fully into PSRAM before playback starts, so cap it to
// something sane (2MB ~= 64s of mono 16-bit 16kHz audio) rather than trusting
// a server-supplied Content-Length unconditionally.
#define MAX_RESPONSE_BYTES   (2 * 1024 * 1024)

#pragma pack(push, 1)
typedef struct {
    char     riff_id[4];
    uint32_t riff_size;
    char     wave_id[4];
    char     fmt_id[4];
    uint32_t fmt_size;
    uint16_t audio_format;
    uint16_t num_channels;
    uint32_t sample_rate;
    uint32_t byte_rate;
    uint16_t block_align;
    uint16_t bits_per_sample;
    char     data_id[4];
    uint32_t data_size;
} wav_header_t;
#pragma pack(pop)

static void fill_wav_header(wav_header_t *h, uint32_t pcm_bytes)
{
    memcpy(h->riff_id, "RIFF", 4);
    memcpy(h->wave_id, "WAVE", 4);
    memcpy(h->fmt_id, "fmt ", 4);
    memcpy(h->data_id, "data", 4);
    h->fmt_size = 16;
    h->audio_format = 1; // PCM
    h->num_channels = 1;
    h->sample_rate = AUDIO_CAPTURE_SAMPLE_RATE;
    h->bits_per_sample = 16;
    h->block_align = h->num_channels * h->bits_per_sample / 8;
    h->byte_rate = h->sample_rate * h->block_align;
    h->data_size = pcm_bytes;
    h->riff_size = 36 + pcm_bytes;
}

// POSTs `body` to `url` and reads the response body into a PSRAM buffer the
// caller must free. Returns ESP_OK only on HTTP 200.
static esp_err_t http_post_and_read(const char *url, const uint8_t *body, size_t body_len, uint8_t **out_resp, size_t *out_resp_len)
{
    *out_resp = NULL;
    *out_resp_len = 0;

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        ESP_LOGE(TAG, "Failed to init HTTP client");
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Content-Type", "audio/wav");

    esp_err_t err = esp_http_client_open(client, body_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open connection to %s: %s", url, esp_err_to_name(err));
        esp_http_client_cleanup(client);
        return err;
    }

    size_t written = 0;
    while (written < body_len) {
        int w = esp_http_client_write(client, (const char *)(body + written), body_len - written);
        if (w < 0) {
            ESP_LOGE(TAG, "Failed writing request body");
            esp_http_client_close(client);
            esp_http_client_cleanup(client);
            return ESP_FAIL;
        }
        written += w;
    }

    int64_t content_length = esp_http_client_fetch_headers(client);
    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGE(TAG, "Server returned HTTP %d", status);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_FAIL;
    }

    size_t cap = MAX_RESPONSE_BYTES;
    if (content_length > 0 && (size_t)content_length < MAX_RESPONSE_BYTES) {
        cap = (size_t)content_length;
    }
    uint8_t *resp = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
    if (!resp) {
        ESP_LOGE(TAG, "Failed to allocate %u byte response buffer", (unsigned)cap);
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_ERR_NO_MEM;
    }

    int total = esp_http_client_read_response(client, (char *)resp, cap);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);

    if (total < 0) {
        ESP_LOGE(TAG, "Failed reading response body");
        free(resp);
        return ESP_FAIL;
    }

    *out_resp = resp;
    *out_resp_len = (size_t)total;
    return ESP_OK;
}

// Finds the "data" sub-chunk of what may be a WAV file, so a server that
// responds with a real WAV (header included -- e.g. Python's `wave` module)
// plays back correctly instead of ~22 header bytes being misread as noise
// up front. Falls back to treating the whole buffer as headerless raw PCM
// if it doesn't start with a RIFF/WAVE container, so a server that already
// implements the "no header" contract keeps working unchanged.
static void find_pcm_payload(const uint8_t *buf, size_t len, const uint8_t **out_pcm, size_t *out_len)
{
    *out_pcm = buf;
    *out_len = len;

    if (len < 12 || memcmp(buf, "RIFF", 4) != 0 || memcmp(buf + 8, "WAVE", 4) != 0) {
        return;
    }

    size_t pos = 12; // first sub-chunk starts right after the 12-byte RIFF/WAVE header
    while (pos + 8 <= len) {
        uint32_t chunk_size;
        memcpy(&chunk_size, buf + pos + 4, sizeof(chunk_size)); // WAV fields are little-endian, same as this chip
        size_t data_start = pos + 8;

        if (memcmp(buf + pos, "data", 4) == 0) {
            size_t avail = len - data_start;
            *out_pcm = buf + data_start;
            *out_len = (chunk_size < avail) ? chunk_size : avail;
            return;
        }

        pos = data_start + chunk_size + (chunk_size & 1); // chunks are padded to even byte boundaries
    }
    // No "data" chunk found before EOF -- leave the raw-PCM fallback set above.
}

// Plays raw mono 16-bit PCM at AUDIO_CAPTURE_SAMPLE_RATE by duplicating it
// into a stereo buffer, since the DAC is opened as a 2-channel device.
static esp_err_t play_mono_pcm(const int16_t *mono, int frames)
{
    int16_t *stereo = heap_caps_malloc((size_t)frames * 2 * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!stereo) {
        return ESP_ERR_NO_MEM;
    }
    for (int i = 0; i < frames; i++) {
        stereo[i * 2 + 0] = mono[i];
        stereo[i * 2 + 1] = mono[i];
    }
    esp_err_t ret = esp_audio_play(stereo, frames * 2 * sizeof(int16_t), portMAX_DELAY);
    free(stereo);
    return ret;
}

esp_err_t voice_assistant_ask(void)
{
    if (!wifi_conn_is_connected()) {
        ESP_LOGW(TAG, "WiFi not connected, skipping");
        return ESP_ERR_INVALID_STATE;
    }

    int16_t *mono_buf = NULL;
    int total_frames = 0;

    led_ring_set_color(255, 0, 255); // magenta: recording
    esp_err_t ret = audio_capture_mono(RECORD_SECONDS, &mono_buf, &total_frames);
    if (ret != ESP_OK) {
        led_ring_flash(255, 0, 0, 2);
        led_ring_off();
        return ret; // feed_Task was never paused if capture itself failed
    }
    // feed_Task stays paused (audio_capture_mono() doesn't resume it) through
    // the upload and response playback below, since playback also competes
    // with it for the shared I2S peripheral -- resumed on every exit path.

    size_t pcm_bytes = (size_t)total_frames * sizeof(int16_t);
    size_t wav_len = sizeof(wav_header_t) + pcm_bytes;
    uint8_t *wav_buf = heap_caps_malloc(wav_len, MALLOC_CAP_SPIRAM);
    if (!wav_buf) {
        ESP_LOGE(TAG, "Failed to allocate WAV upload buffer");
        free(mono_buf);
        mic_speech_pause_feed(false);
        led_ring_flash(255, 0, 0, 2);
        led_ring_off();
        return ESP_ERR_NO_MEM;
    }
    wav_header_t hdr;
    fill_wav_header(&hdr, (uint32_t)pcm_bytes);
    memcpy(wav_buf, &hdr, sizeof(hdr));
    memcpy(wav_buf + sizeof(hdr), mono_buf, pcm_bytes);
    free(mono_buf);

    char url[APP_CONFIG_URL_MAX_LEN];
    app_config_get_llm_url(url, sizeof(url));

    ESP_LOGI(TAG, "Uploading %u bytes to %s...", (unsigned)wav_len, url);
    led_ring_set_color(255, 255, 0); // yellow: uploading / waiting for response

    uint8_t *resp_buf = NULL;
    size_t resp_len = 0;
    ret = http_post_and_read(url, wav_buf, wav_len, &resp_buf, &resp_len);
    free(wav_buf);

    if (ret != ESP_OK) {
        mic_speech_pause_feed(false);
        led_ring_flash(255, 0, 0, 2);
        led_ring_off();
        return ret;
    }

    const uint8_t *pcm_data = NULL;
    size_t pcm_len = 0;
    find_pcm_payload(resp_buf, resp_len, &pcm_data, &pcm_len);

    if (pcm_len < sizeof(int16_t)) {
        ESP_LOGW(TAG, "Empty or too-short response (%u bytes)", (unsigned)pcm_len);
        free(resp_buf);
        mic_speech_pause_feed(false);
        led_ring_off();
        return ESP_OK;
    }

    int resp_frames = pcm_len / sizeof(int16_t);
    ESP_LOGI(TAG, "Playing back %d frame response...", resp_frames);
    led_ring_set_color(0, 128, 255); // blue: playing back the response
    play_mono_pcm((const int16_t *)pcm_data, resp_frames);
    free(resp_buf);

    mic_speech_pause_feed(false);
    led_ring_off();
    ESP_LOGI(TAG, "Done");
    return ESP_OK;
}
