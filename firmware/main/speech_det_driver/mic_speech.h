#pragma once

#include <stdbool.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"


#ifdef __cplusplus
extern "C" {
#endif


// esp-sr事件
typedef enum {
    ESP_SR_EVT_AWAKEN,  
    ESP_SR_EVT_CMD,
    ESP_SR_EVT_CMD_TIMEOUT
} esp_sr_rec_event_t;


typedef union { 
    uint8_t awaken_channel;    // 唤醒通道
    uint8_t sr_cmd;             // 识别命令ID
} esp_sr_evt_data_t;


// 回调函数类型
typedef void (*esp_sr_event_callback_t)(esp_sr_rec_event_t event,esp_sr_evt_data_t evt_data, void *user_data);

void Speech_Init(void);
esp_err_t Speech_register_callback(esp_sr_event_callback_t callback);

// Stops/resumes feed_Task's mic reads so another task can safely read the
// mic directly (e.g. to record audio) without racing feed_Task for the codec.
void mic_speech_pause_feed(bool pause);

#ifdef __cplusplus
}
#endif
