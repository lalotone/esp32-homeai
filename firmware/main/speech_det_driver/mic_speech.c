#include "mic_speech.h"

#include "esp_wn_iface.h"
#include "esp_wn_models.h"
#include "esp_afe_sr_iface.h"
#include "esp_afe_sr_models.h"
#include "esp_mn_iface.h"
#include "esp_mn_models.h"
#include "esp_mn_speech_commands.h"
#include "model_path.h"
#include "esp_process_sdkconfig.h"

#include "bsp_board.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "esp_task_wdt.h"

static const char *TAG = "App/Speech";

// Second wake phrase, "Hey Rocky", next to WakeNet's "Alexa". There is no WakeNet model for it, so it runs on
// MultiNet7 (English speech commands: the phrase is plain text, no training). MultiNet is built to listen for a
// few seconds after a wake word; here it listens all the time, in back-to-back windows, so it is less robust
// than "Alexa". Every MultiNet hit is logged with its probability to tune this against false wakes.
#define ROCKY_COMMAND_ID 1
#define ROCKY_WINDOW_MS  6000   // MultiNet times out after this; we restart it right away and keep listening
static const char *rocky_phrases[] = {
    "hey rocky",
    "hey roki",                 // spelling variants: MultiNet7 turns text into English phonemes, so these
    "hi rocky",                 // widen it a bit for a Spanish accent ("hey" heard as "hi", a shorter "o")
};


int wakeup_flag = 0;
static const esp_afe_sr_iface_t *afe_handle = NULL;
static volatile int task_flag = 0;
static volatile bool feed_paused = false;
srmodel_list_t *models = NULL;
static esp_sr_event_callback_t spench_callback =  NULL;

void mic_speech_pause_feed(bool pause)
{
    feed_paused = pause;
}

void feed_Task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;
    int audio_chunksize = afe_handle->get_feed_chunksize(afe_data);
    int nch = afe_handle->get_feed_channel_num(afe_data);
    int feed_channel = esp_get_feed_channel();
    assert(nch == feed_channel);
    //int16_t *i2s_buff = malloc(audio_chunksize * sizeof(int16_t) * feed_channel);
    int16_t *i2s_buff = heap_caps_malloc(audio_chunksize * sizeof(int16_t) * feed_channel,MALLOC_CAP_SPIRAM);

    assert(i2s_buff);

    esp_task_wdt_add(NULL);      // 先订阅当前任务
    while (task_flag) {
        if (feed_paused) {
            vTaskDelay(pdMS_TO_TICKS(10));
            esp_task_wdt_reset();
            continue;
        }
        esp_get_feed_data(true, i2s_buff, audio_chunksize * sizeof(int16_t) * feed_channel);
        afe_handle->feed(afe_data, i2s_buff);
        esp_task_wdt_reset();
    }
    if (i2s_buff) {
        free(i2s_buff);
        i2s_buff = NULL;
    }
    vTaskDelete(NULL);
}

static model_iface_data_t *rocky_init(esp_mn_iface_t **multinet_out, int afe_chunksize)
{
    char *mn_name = esp_srmodel_filter(models, ESP_MN_PREFIX, ESP_MN_ENGLISH);
    if (!mn_name) {
        ESP_LOGW(TAG, "No English MultiNet model in the 'model' partition: \"Hey Rocky\" disabled, \"Alexa\" only");
        return NULL;
    }
    esp_mn_iface_t *multinet = esp_mn_handle_from_name(mn_name);
    model_iface_data_t *model_data = multinet->create(mn_name, ROCKY_WINDOW_MS);
    // All weights in PSRAM where the model supports choosing (MultiNet7 leaves this NULL: calling it panics).
    if (multinet->switch_loader_mode) {
        model_data = multinet->switch_loader_mode(model_data, ESP_MN_LOAD_FROM_PSRAM);
    }
    assert(multinet->get_samp_chunksize(model_data) == afe_chunksize);

    esp_mn_commands_alloc(multinet, model_data);
    for (size_t i = 0; i < sizeof(rocky_phrases) / sizeof(rocky_phrases[0]); i++) {
        esp_mn_commands_add(ROCKY_COMMAND_ID, rocky_phrases[i]);
    }
    esp_mn_error_t *err = esp_mn_commands_update();
    if (err) {
        for (int i = 0; i < err->num; i++) {
            ESP_LOGW(TAG, "MultiNet rejected phrase \"%s\"", err->phrases[i]->string);
        }
    }
    ESP_LOGI(TAG, "\"Hey Rocky\" on %s:", mn_name);
    multinet->print_active_speech_commands(model_data);
    *multinet_out = multinet;
    return model_data;
}

void detect_Task(void *arg)
{
    esp_afe_sr_data_t *afe_data = arg;
    esp_mn_iface_t *multinet = NULL;
    model_iface_data_t *mn_data = rocky_init(&multinet, afe_handle->get_fetch_chunksize(afe_data));

    // A reply is downloaded whole into PSRAM (up to 2 MB), so this is the headroom left for it.
    ESP_LOGI(TAG, "Speech ready, PSRAM free: %u KB (largest block %u KB)",
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024),
             (unsigned)(heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM) / 1024));

    esp_task_wdt_add(NULL);      // 先订阅当前任务

    while (task_flag) {
        afe_fetch_result_t* res = afe_handle->fetch(afe_data);
        if (!res || res->ret_value == ESP_FAIL) {
            // Transient: right after an ask round-trip the AFE ring can be briefly
            // starved (feed_Task was paused while we recorded/uploaded/played).
            // Back off and retry instead of tearing the detector down on one bad fetch.
            vTaskDelay(pdMS_TO_TICKS(5));
            esp_task_wdt_reset();
            continue;
        }

        // Fire on a verified wake: single-channel is WAKENET_DETECTED, while a
        // multi-channel AFE must wait for WAKENET_CHANNEL_VERIFIED (channel confirmed).
        bool woken = false;
        if (res->raw_data_channels == 1 && res->wakeup_state == WAKENET_DETECTED) {
            woken = true;
        } else if (res->raw_data_channels > 1 && res->wakeup_state == WAKENET_CHANNEL_VERIFIED) {
            woken = true;
        }

        // "Hey Rocky": MultiNet on the same AFE output. A timeout just means nobody said it in this window.
        bool rocky = false;
        if (mn_data) {
            esp_mn_state_t mn_state = multinet->detect(mn_data, res->data);
            if (mn_state == ESP_MN_STATE_DETECTED) {
                esp_mn_results_t *r = multinet->get_results(mn_data);
                ESP_LOGI(TAG, "MultiNet heard \"%s\" (command %d, prob %.3f)", r->string, r->command_id[0], r->prob[0]);
                rocky = r->num > 0 && r->command_id[0] == ROCKY_COMMAND_ID;
                multinet->clean(mn_data);
            } else if (mn_state == ESP_MN_STATE_TIMEOUT) {
                multinet->clean(mn_data);
            }
        }

        // The wakeup_flag == 0 guard is the real fix: WakeNet evaluates a ~1.5s
        // sliding window, so a single utterance can produce several consecutive
        // "detected" results already queued up in the AFE pipeline by the time
        // disable_wakenet() below takes effect -- disabling alone only stops
        // *future* inference, it doesn't un-queue results already computed.
        // Gating on wakeup_flag makes re-firing impossible regardless of timing.
        if ((woken || rocky) && wakeup_flag == 0) {
            afe_handle->disable_wakenet(afe_data);
            wakeup_flag = 1;

            esp_sr_evt_data_t evtdata;
            evtdata.awaken_channel = (woken && res->raw_data_channels > 1) ? res->trigger_channel_id : 0;
            ESP_LOGI(TAG, "Wake word detected (%s, ch=%d), capturing + asking", woken ? "Alexa" : "Hey Rocky",
                     (int)evtdata.awaken_channel);
            if (spench_callback != NULL) {
                // Synchronous: the callback runs the whole ask round-trip (VAD
                // capture + upload + playback) before returning, so the wake word
                // stays disabled and cannot re-fire in the middle of the request.
                // A conversation (with follow-ups) lasts well past the watchdog's
                // 5 s, so this task leaves the watchdog for its duration.
                esp_task_wdt_delete(NULL);
                spench_callback(ESP_SR_EVT_AWAKEN, evtdata, NULL);
                esp_task_wdt_add(NULL);
            }

            afe_handle->enable_wakenet(afe_data);
            if (mn_data) {
                multinet->clean(mn_data); // drop whatever it half-heard before/while answering
            }
            wakeup_flag = 0;
        }

        esp_task_wdt_reset();
    }
    if (mn_data) {
        multinet->destroy(mn_data);
    }
    printf("detect exit\n");
    vTaskDelete(NULL);
}

void Speech_Init(void)
{
    models = esp_srmodel_init("model"); // partition label defined in partitions.csv
    afe_config_t *afe_config = afe_config_init(esp_get_input_format(), models, AFE_TYPE_SR, AFE_MODE_LOW_COST);
    afe_config->ns_init = false;
    afe_config->vad_init = false;
    afe_handle = esp_afe_handle_from_config(afe_config);
    esp_afe_sr_data_t *afe_data = afe_handle->create_from_config(afe_config);
    afe_config_free(afe_config);

    // Lower the wake word detection threshold (range 0.4-0.9999, default ~0.6-0.65)
    // to trade a few extra false triggers for a much higher hit rate on "Alexa".
    afe_handle->set_wakenet_threshold(afe_data, 1, 0.5);

    task_flag = 1;
    xTaskCreatePinnedToCore(&detect_Task, "detect", 8 * 1024, (void*)afe_data, 5, NULL, 1);
    xTaskCreatePinnedToCore(&feed_Task, "feed", 8 * 1024, (void*)afe_data, 5, NULL, 0);
}

// 注册回调
esp_err_t Speech_register_callback(esp_sr_event_callback_t callback) 
{
    if (!callback) return ESP_ERR_INVALID_ARG;
    spench_callback = callback;
    return ESP_OK;
}