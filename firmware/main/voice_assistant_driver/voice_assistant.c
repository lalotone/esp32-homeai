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
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "voice_assistant";

// A real server does STT -> LLM -> TTS as three sequential blocking calls
// before it can respond at all, which can easily take longer than a plain
// API round trip -- give it real headroom rather than timing out mid-chain.
// 180s: a CPU-only server using the Rocky voice (Qwen3-TTS) takes ~60-90s for the TTS
// step alone. A GPU-hosted voice answers in a few seconds, well inside this.
#define HTTP_TIMEOUT_MS      180000
// Response is played fully into PSRAM before playback starts, so cap it to
// something sane (2MB ~= 64s of mono 16-bit 16kHz audio) rather than trusting
// a server-supplied Content-Length unconditionally.
#define MAX_RESPONSE_BYTES   (2 * 1024 * 1024)

// Timers and reminders: the speaker can't be called, so while idle it polls the API's
// GET /pending (next to the /talk URL) and plays whatever announcement comes back;
// 204 = nothing due. The poll answer includes synthesizing Rocky's line (~2-3 s).
#define ANNOUNCE_POLL_MS        3000
#define ANNOUNCE_HTTP_TIMEOUT_MS 15000

// One conversation with the speaker at a time: a question (voice_assistant_ask) and an
// announcement both need the mic paused and the DAC, so they never overlap.
static SemaphoreHandle_t s_speaker_lock;

// Follow-up mode: after each answer the mic stays open this long, so the user can keep
// talking without the wake word (the API keeps the conversation's context). Silence in
// that window, or an answer that is just the API's "nothing heard" blip, ends it.
#define FOLLOWUP_WINDOW_MS      6000
#define FOLLOWUP_MAX_TURNS      5     // follow-ups per wake, so a TV can't keep it talking forever
#define FOLLOWUP_ECHO_GAP_MS    300   // let the speaker's last syllable die out before listening
#define FOLLOWUP_MIN_SPEECH     3     // ~100 ms of speech to count (not a click or an echo tail)
#define NOTHING_HEARD_MAX_S     1.0f  // the API answers silence with a ~0.4 s silent clip

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

// GETs `url`; on HTTP 200 reads the body into a PSRAM buffer the caller must free.
// *out_status is the HTTP status (0 if the request itself failed).
static esp_err_t http_get_and_read(const char *url, int timeout_ms, int *out_status, uint8_t **out_resp, size_t *out_resp_len)
{
    *out_status = 0;
    *out_resp = NULL;
    *out_resp_len = 0;

    esp_http_client_config_t config = {
        .url = url,
        .method = HTTP_METHOD_GET,
        .timeout_ms = timeout_ms,
    };
    esp_http_client_handle_t client = esp_http_client_init(&config);
    if (!client) {
        return ESP_FAIL;
    }
    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        esp_http_client_cleanup(client);
        return err;
    }
    int64_t content_length = esp_http_client_fetch_headers(client);
    *out_status = esp_http_client_get_status_code(client);
    if (*out_status != 200 || content_length <= 0) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_OK; // 204 and friends: nothing to play
    }

    size_t cap = (size_t)content_length < MAX_RESPONSE_BYTES ? (size_t)content_length : MAX_RESPONSE_BYTES;
    uint8_t *resp = heap_caps_malloc(cap, MALLOC_CAP_SPIRAM);
    if (!resp) {
        esp_http_client_close(client);
        esp_http_client_cleanup(client);
        return ESP_ERR_NO_MEM;
    }
    int total = esp_http_client_read_response(client, (char *)resp, cap);
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    if (total <= 0) {
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

// Plays raw mono 16-bit PCM at AUDIO_CAPTURE_SAMPLE_RATE, duplicated to
// stereo since the DAC is opened as a 2-channel device. Chunk by chunk: a
// stereo copy of the whole reply needs twice its size in PSRAM, which a long
// answer (e.g. the news, ~30 s = ~2 MB stereo) no longer gets next to
// MultiNet7's weights -- the allocation failed and the reply was silently lost.
#define PLAY_CHUNK_FRAMES 4096  // 256 ms per chunk; blocking I2S writes keep it gapless
static esp_err_t play_mono_pcm(const int16_t *mono, int frames)
{
    int16_t *stereo = heap_caps_malloc(PLAY_CHUNK_FRAMES * 2 * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!stereo) {
        return ESP_ERR_NO_MEM;
    }
    esp_err_t ret = ESP_OK;
    for (int done = 0; done < frames && ret == ESP_OK; done += PLAY_CHUNK_FRAMES) {
        int n = (frames - done < PLAY_CHUNK_FRAMES) ? frames - done : PLAY_CHUNK_FRAMES;
        for (int i = 0; i < n; i++) {
            stereo[i * 2 + 0] = mono[done + i];
            stereo[i * 2 + 1] = mono[done + i];
        }
        ret = esp_audio_play(stereo, n * 2 * sizeof(int16_t), portMAX_DELAY);
    }
    free(stereo);
    return ret;
}

static esp_err_t ask_locked(void);

esp_err_t voice_assistant_ask(void)
{
    if (!s_speaker_lock) {
        return ask_locked(); // announcements not started: nothing to share the speaker with
    }
    xSemaphoreTake(s_speaker_lock, portMAX_DELAY); // waits out an announcement that's playing
    esp_err_t ret = ask_locked();
    xSemaphoreGive(s_speaker_lock);
    return ret;
}

// One question and its answer. A follow-up turn only listens for FOLLOWUP_WINDOW_MS and
// uploads nothing if the user stays quiet. *out_answered: a real answer was played (so a
// further follow-up makes sense).
static esp_err_t one_turn(bool followup, bool *out_answered)
{
    *out_answered = false;
    int16_t *mono_buf = NULL;
    int total_frames = 0;
    esp_err_t ret;

    led_ring_set_color(255, 0, 255); // magenta: recording (also "you can talk now" in a follow-up)
    if (followup) {
        vTaskDelay(pdMS_TO_TICKS(FOLLOWUP_ECHO_GAP_MS));
        bool heard = false;
        ret = audio_capture_vad_wait(FOLLOWUP_WINDOW_MS, FOLLOWUP_MIN_SPEECH, &mono_buf, &total_frames, &heard);
        if (ret == ESP_OK && !heard) {
            ESP_LOGI(TAG, "No follow-up: back to the wake word");
            free(mono_buf);
            mic_speech_pause_feed(false);
            led_ring_off();
            return ESP_OK;
        }
    } else {
        // Variable-length VAD capture: record until the user stops talking (or the
        // cap elapses). If nothing was said, this still returns audio -- the API is
        // the one that decides it's silence and skips the pipeline.
        ret = audio_capture_vad(&mono_buf, &total_frames);
    }
    if (ret != ESP_OK) {
        mic_speech_pause_feed(false);
        led_ring_flash(255, 0, 0, 2);
        led_ring_off();
        return ret;
    }
    // feed_Task stays paused (audio_capture_vad() doesn't resume it) through
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
    ESP_LOGI(TAG, "Playing back %d frame response (%.1f s, %u KB PSRAM free)...", resp_frames,
             resp_frames / (float)AUDIO_CAPTURE_SAMPLE_RATE, (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
    led_ring_set_color(0, 128, 255); // blue: playing back the response
    ret = play_mono_pcm((const int16_t *)pcm_data, resp_frames);
    free(resp_buf);
    if (ret != ESP_OK) {
        ESP_LOGE(TAG, "Playback failed: %s", esp_err_to_name(ret));
        mic_speech_pause_feed(false);
        led_ring_flash(255, 0, 0, 2);
        led_ring_off();
        return ret;
    }

    mic_speech_pause_feed(false);
    led_ring_off();
    *out_answered = resp_frames / (float)AUDIO_CAPTURE_SAMPLE_RATE > NOTHING_HEARD_MAX_S;
    ESP_LOGI(TAG, "Done");
    return ESP_OK;
}

static esp_err_t ask_locked(void)
{
    if (!wifi_conn_is_connected()) {
        ESP_LOGW(TAG, "WiFi not connected, skipping");
        return ESP_ERR_INVALID_STATE;
    }
    bool answered = false;
    esp_err_t ret = one_turn(false, &answered);
    for (int i = 0; ret == ESP_OK && answered && i < FOLLOWUP_MAX_TURNS; i++) {
        ESP_LOGI(TAG, "Listening for a follow-up (%d/%d)...", i + 1, FOLLOWUP_MAX_TURNS);
        ret = one_turn(true, &answered);
    }
    return ret;
}

// The announcement URL is the /talk URL with "/talk" swapped for "/pending"
// (or "/pending" appended to the URL's directory if it doesn't end in /talk).
static void pending_url(char *out, size_t out_size)
{
    char talk[APP_CONFIG_URL_MAX_LEN];
    app_config_get_llm_url(talk, sizeof(talk));
    char *host = strstr(talk, "://");
    host = host ? host + 3 : talk;
    char *slash = strrchr(host, '/'); // last '/' of the path, never the scheme's
    if (slash) {
        *slash = '\0';
    }
    snprintf(out, out_size, "%s/pending", talk);
}

static void announce_task(void *arg)
{
    (void)arg;
    char url[APP_CONFIG_URL_MAX_LEN + 16];
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(ANNOUNCE_POLL_MS));
        if (!wifi_conn_is_connected() || xSemaphoreTake(s_speaker_lock, 0) != pdTRUE) {
            continue; // offline, or a question is in progress: try on the next tick
        }
        pending_url(url, sizeof(url));
        int status = 0;
        uint8_t *resp = NULL;
        size_t resp_len = 0;
        esp_err_t err = http_get_and_read(url, ANNOUNCE_HTTP_TIMEOUT_MS, &status, &resp, &resp_len);
        if (err == ESP_OK && resp) {
            const uint8_t *pcm = NULL;
            size_t pcm_len = 0;
            find_pcm_payload(resp, resp_len, &pcm, &pcm_len);
            if (pcm_len >= sizeof(int16_t)) {
                ESP_LOGI(TAG, "Announcement (%.1f s)", pcm_len / 2 / (float)AUDIO_CAPTURE_SAMPLE_RATE);
                mic_speech_pause_feed(true); // playback shares the I2S peripheral with the mic feed
                led_ring_set_color(0, 255, 64); // green: Rocky is announcing a timer/reminder
                err = play_mono_pcm((const int16_t *)pcm, pcm_len / sizeof(int16_t));
                if (err != ESP_OK) {
                    ESP_LOGE(TAG, "Announcement playback failed: %s", esp_err_to_name(err));
                }
                led_ring_off();
                mic_speech_pause_feed(false);
            }
            free(resp);
        } else if (err != ESP_OK) {
            ESP_LOGD(TAG, "Poll %s failed: %s", url, esp_err_to_name(err)); // server down/restarting: quiet retry
        }
        xSemaphoreGive(s_speaker_lock);
    }
}

esp_err_t voice_assistant_start_announcements(void)
{
    s_speaker_lock = xSemaphoreCreateMutex();
    if (!s_speaker_lock) {
        return ESP_ERR_NO_MEM;
    }
    if (xTaskCreatePinnedToCore(announce_task, "announce", 6 * 1024, NULL, 4, NULL, 0) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}
