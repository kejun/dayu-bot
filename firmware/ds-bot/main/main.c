/* ESP32 terminal: serialized audio uploads and acknowledged task cancellation. */
#include <stddef.h>
#include <assert.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "lvgl.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"
#include "cJSON.h"
#include "nvs_flash.h"
#include "esp_heap_caps.h"
#include "bluebot_assets.h"
#include "bluebot_player.h"
#include "dsbot_font_32.h"
#include "audio.h"
#include "buttons.h"
#include "input_policy.h"
#include "net.h"
#include "axp2101.h"

static const char *TAG = "ds-bot";
static bluebot_player_t s_pet;
static lv_obj_t *s_face, *s_label, *s_net_label;

typedef enum {
    UI_IDLE, UI_RECORDING, UI_PROCESSING, UI_SPEAKING, UI_OK, UI_FAIL,
    UI_CANCELLING, UI_CANCELLED, UI_CANCEL_UNCONFIRMED,
    UI_LISTENING,
} ui_state_t;
static const bluebot_state_t k_face[] = {
    BLUEBOT_IDLE, BLUEBOT_LISTEN, BLUEBOT_THINK, BLUEBOT_SPEAK,
    BLUEBOT_HAPPY, BLUEBOT_SAD, BLUEBOT_THINK, BLUEBOT_IDLE, BLUEBOT_SAD,
    BLUEBOT_LISTEN,
};
static const char *k_text[] = {
    "待机", "录音中…", "处理中…", "播报中…", "完成", "失败",
    "正在取消…", "任务已经取消", "取消未确认",
    "请说话…",
};

/* Protect task identity, button intent and UI ordering across WS / audio / key tasks. */
static SemaphoreHandle_t s_state_lock;
static ui_state_t s_state = UI_IDLE;
static uint32_t s_ui_revision;
static bool s_screen_on = true;
static bool s_task_active, s_cancel_sent, s_ptt_busy, s_ptt_recording;
static bool s_question_waiting, s_answer_listening;
static uint32_t s_question_seq;
static char s_task_id[96];
static uint32_t s_task_epoch, s_boot_id, s_req_seq, s_wake_seq;
static TickType_t s_cancel_started_at;
#define CANCEL_ACK_TIMEOUT_MS 15000
static input_policy_t s_input;

typedef enum { PTT_PRESS, PTT_RELEASE } ptt_evt_t;
static QueueHandle_t s_ptt_q;
typedef struct {
    uint8_t *pcm;
    size_t len;
    uint32_t play_generation, speech_generation;
    net_speech_t speech;
} tts_item_t;
static QueueHandle_t s_tts_q;

/* Caller holds s_state_lock. */
static void ui_set_state(ui_state_t state)
{
    s_state = state;
    ++s_ui_revision;
    bsp_display_lock(-1);
    bluebot_player_set_state(&s_pet, k_face[state], lv_tick_get());
    lv_label_set_text(s_label, k_text[state]);
    bsp_display_unlock();
}

static void ui_set_net_locked(const char *text)
{
    bsp_display_lock(-1);
    lv_label_set_text(s_net_label, text);
    bsp_display_unlock();
}

static void on_pwrkey(void)
{
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    s_screen_on = !s_screen_on;
    bsp_display_brightness_set(s_screen_on ? 100 : 0);
    xSemaphoreGive(s_state_lock);
}

static void stop_speaking(void)
{
    net_discard_speech();
    audio_play_cancel();
    tts_item_t item;
    while (s_tts_q && xQueueReceive(s_tts_q, &item, 0) == pdTRUE) free(item.pcm);
}

static void task_started(const char *rid)
{
    if (!s_task_active || strcmp(s_task_id, rid)) {
        ++s_task_epoch;
        s_cancel_sent = false;
    }
    strlcpy(s_task_id, rid, sizeof(s_task_id));
    s_task_active = true;
}

static const char *json_string(cJSON *root, const char *key)
{
    cJSON *value = cJSON_GetObjectItem(root, key);
    return cJSON_IsString(value) ? value->valuestring : "";
}

static void on_net_msg(const char *type, const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) return;
    const char *tt = json_string(root, "t");
    if (!*tt) tt = type;
    const char *rid = json_string(root, "request_id");
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    if (!strcmp(tt, "stop_speak")) {
        stop_speaking();
    } else if (!strcmp(tt, "wake")) {
        s_screen_on = true;
        bsp_display_brightness_set(100);
        if (!s_task_active && !s_ptt_busy) ui_set_state(UI_RECORDING);
    } else if (!strcmp(tt, "welcome")) {
        ui_set_net_locked("已连接主机");
    } else if (!strcmp(tt, "status")) {
        const char *st = json_string(root, "state");
        bool active = !strcmp(st, "processing") || !strcmp(st, "need_user") ||
                      !strcmp(st, "cancelling") || !strcmp(st, "recognizing");
        if (active && *rid && (!s_task_active || !strcmp(s_task_id, rid))) {
            task_started(rid);
            if (!strcmp(st, "cancelling")) {
                if (!s_cancel_sent) s_cancel_started_at = xTaskGetTickCount();
                s_cancel_sent = true;
                s_question_waiting = s_answer_listening = false;
                audio_wake_pause(false);
                net_cancel_audio_uploads();
                ui_set_state(UI_CANCELLING);
            } else if (!s_cancel_sent) {
                if (!strcmp(st, "processing")) s_question_waiting = s_answer_listening = false;
                audio_wake_pause(!strcmp(st, "recognizing"));
                ui_set_state(UI_PROCESSING);
            }
        } else if (!strcmp(st, "cancelled") && *rid && !strcmp(s_task_id, rid)) {
            s_task_active = false;
            s_cancel_sent = false;
            s_question_waiting = s_answer_listening = false;
            audio_wake_pause(false);
            ui_set_state(UI_CANCELLED);
        } else if (!s_task_active && !s_ptt_busy && !strcmp(st, "recording")) {
            /* Background VAD must not replace an active phone task's state. */
            ui_set_state(UI_RECORDING);
        }
    } else if (!strcmp(tt, "question")) {
        if (*rid && (!s_task_active || !strcmp(s_task_id, rid))) {
            task_started(rid);
            if (!s_cancel_sent) {
                stop_speaking();
                cJSON *seq = cJSON_GetObjectItem(root, "question_seq");
                s_question_seq = cJSON_IsNumber(seq) ? (uint32_t)seq->valuedouble : 0;
                s_question_waiting = true;
                s_answer_listening = false;
                audio_wake_pause(true);
                ui_set_state(UI_SPEAKING);
                ESP_LOGI(TAG, "question speaking: %s seq=%lu", rid, (unsigned long)s_question_seq);
            }
        }
    } else if (!strcmp(tt, "listen")) {
        cJSON *seq = cJSON_GetObjectItem(root, "question_seq");
        if (s_task_active && s_question_waiting && !s_cancel_sent && !strcmp(s_task_id, rid) &&
            cJSON_IsNumber(seq) && s_question_seq == (uint32_t)seq->valuedouble) {
            s_answer_listening = true;
            audio_wake_pause(false);
            ui_set_state(UI_LISTENING);
            ESP_LOGI(TAG, "question listening: %s seq=%lu", rid, (unsigned long)s_question_seq);
        }
    } else if (!strcmp(tt, "result")) {
        if (*rid && (!*s_task_id || !strcmp(s_task_id, rid))) {
            strlcpy(s_task_id, rid, sizeof(s_task_id));
            bool was_cancelling = s_cancel_sent || s_state == UI_CANCEL_UNCONFIRMED;
            s_task_active = false;
            s_cancel_sent = false;
            s_question_waiting = s_answer_listening = false;
            audio_wake_pause(false);
            const char *kind = json_string(root, "kind");
            if (!strcmp(kind, "cancelled")) {
                ui_set_state(UI_CANCELLED);
            } else if (was_cancelling && !strcmp(kind, "unknown")) {
                ui_set_state(UI_CANCEL_UNCONFIRMED);
            } else {
                ui_set_state(!strcmp(kind, "success") ? UI_OK : UI_FAIL);
            }
        }
    } else if (!strcmp(tt, "notice")) {
        if (!strcmp(json_string(root, "level"), "error")) {
            if (s_cancel_sent) ui_set_state(UI_CANCEL_UNCONFIRMED);
            else if (!s_task_active) ui_set_state(UI_FAIL);
        }
    }
    xSemaphoreGive(s_state_lock);
    cJSON_Delete(root);
}

/* Button edges only classify intent. Upload/playback cannot delay the hold timer. */
static void on_button(int gpio, bool pressed)
{
    int key = gpio == BTN_GPIO_KEY3 ? 0 : 1;
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    input_action_t action = input_policy_edge(&s_input, key, pressed,
            s_task_active, s_task_epoch, !s_ptt_busy,
            (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS));
    if (action == INPUT_RECORD_START) {
        s_ptt_busy = true;
        s_ptt_recording = true;
        stop_speaking();
    }
    if (action == INPUT_RECORD_START || action == INPUT_RECORD_STOP) {
        ptt_evt_t event = action == INPUT_RECORD_START ? PTT_PRESS : PTT_RELEASE;
        if (xQueueSend(s_ptt_q, &event, 0) != pdTRUE) {
            /* Only one owner can enqueue a press/release pair; queue overflow is an error. */
            ESP_LOGE(TAG, "PTT event queue full");
        }
    }
    xSemaphoreGive(s_state_lock);
}

static void cancel_task(void *arg)
{
    (void)arg;
    while (1) {
        char rid[sizeof(s_task_id)] = "";
        uint32_t epoch = 0;
        bool send = false;
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        if (s_task_active && s_cancel_sent &&
            xTaskGetTickCount() - s_cancel_started_at >= pdMS_TO_TICKS(CANCEL_ACK_TIMEOUT_MS)) {
            s_cancel_sent = false;
            ui_set_state(UI_CANCEL_UNCONFIRMED);
            /* Holding stays one-shot; release and press again to retry. */
        }
        input_action_t action = input_policy_poll(&s_input, s_task_active,
                s_task_epoch, (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS));
        if (action == INPUT_CANCEL && !s_cancel_sent) {
            s_cancel_sent = true;
            s_cancel_started_at = xTaskGetTickCount();
            epoch = s_task_epoch;
            strlcpy(rid, s_task_id, sizeof(rid));
            stop_speaking();
            s_question_waiting = s_answer_listening = false;
            audio_wake_pause(false);
            ui_set_state(UI_CANCELLING);
            send = true;
        }
        xSemaphoreGive(s_state_lock);
        if (send) {
            char msg[192];
            snprintf(msg, sizeof(msg), "{\"v\":\"1\",\"t\":\"cancel\",\"request_id\":\"%s\"}", rid);
            esp_err_t err = net_send_json(msg);
            /* Send cancel before aborting the upload, so the host still knows its ID. */
            if (err == ESP_OK) net_cancel_audio_uploads();
            if (err != ESP_OK) {
                xSemaphoreTake(s_state_lock, portMAX_DELAY);
                if (s_task_active && s_task_epoch == epoch) {
                    s_cancel_sent = false;
                    ui_set_state(UI_CANCEL_UNCONFIRMED);
                }
                xSemaphoreGive(s_state_lock);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

static void ptt_task(void *arg)
{
    (void)arg;
    ptt_evt_t event;
    bool recording = false;
    char rid[48] = "";
    while (1) {
        if (xQueueReceive(s_ptt_q, &event, portMAX_DELAY) != pdTRUE) continue;
        if (event == PTT_PRESS) {
            audio_wake_pause(true);
            snprintf(rid, sizeof(rid), "r%08lx-%lu", (unsigned long)s_boot_id, (unsigned long)++s_req_seq);
            recording = audio_record_start() == ESP_OK;
            xSemaphoreTake(s_state_lock, portMAX_DELAY);
            s_ptt_recording = recording;
            if (!s_task_active) ui_set_state(recording ? UI_RECORDING : UI_FAIL);
            xSemaphoreGive(s_state_lock);
            continue;
        }
        size_t len = recording ? audio_record_stop() : 0;
        recording = false;
        bool upload = false;
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        s_ptt_recording = false;
        if (len > AUDIO_RATE_HZ * 2 / 5 && !s_task_active && net_connected()) {
            task_started(rid); /* Include STT and upload in the cancellable busy interval. */
            ui_set_state(UI_PROCESSING);
            upload = true;
        }
        bool offline_play = len > AUDIO_RATE_HZ * 2 / 5 && !s_task_active && !net_connected();
        xSemaphoreGive(s_state_lock);
        if (upload) {
            esp_err_t err = net_send_audio_clip(rid, "task", audio_record_data(), len);
            if (err != ESP_OK) {
                xSemaphoreTake(s_state_lock, portMAX_DELAY);
                if (s_task_active && !s_cancel_sent && s_state != UI_CANCEL_UNCONFIRMED && !strcmp(s_task_id, rid)) {
                    s_task_active = false;
                    ui_set_state(UI_FAIL);
                }
                xSemaphoreGive(s_state_lock);
            }
        } else if (offline_play) {
            audio_play(audio_record_data(), len);
        }
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        s_ptt_busy = false;
        if (!s_task_active && s_state == UI_RECORDING) ui_set_state(UI_IDLE);
        xSemaphoreGive(s_state_lock);
        /* Processing and need_user keep VAD alive for spoken cancellation. */
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        audio_wake_pause(s_question_waiting && !s_answer_listening);
        xSemaphoreGive(s_state_lock);
    }
}

static void on_wake_clip(const void *pcm, size_t len)
{
    if (!net_connected()) return;
    char rid[48], answer_to[sizeof(s_task_id)] = "";
    uint32_t question_seq = 0;
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    if (s_question_waiting) {
        if (!s_answer_listening || s_cancel_sent) {
            xSemaphoreGive(s_state_lock);
            return;
        }
        strlcpy(answer_to, s_task_id, sizeof(answer_to));
        question_seq = s_question_seq;
        s_answer_listening = false;
        audio_wake_pause(true);
        ui_set_state(UI_PROCESSING);
    }
    xSemaphoreGive(s_state_lock);
    snprintf(rid, sizeof(rid), "w%08lx-%lu", (unsigned long)s_boot_id, (unsigned long)++s_wake_seq);
    esp_err_t err = *answer_to ? net_send_answer_clip(rid, answer_to, question_seq, pcm, len) :
                                net_send_audio_clip(rid, "wake", pcm, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "wake upload failed: %s", esp_err_to_name(err));
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        if (*answer_to && s_question_waiting && !s_cancel_sent &&
            !strcmp(answer_to, s_task_id) && question_seq == s_question_seq) {
            s_answer_listening = true;
            ui_set_state(UI_LISTENING);
            audio_wake_pause(false);
        }
        xSemaphoreGive(s_state_lock);
    }
}

static void speech_done(const net_speech_t *speech, bool ok)
{
    if (!*speech->request_id) return;
    cJSON *msg = cJSON_CreateObject();
    if (!msg) return;
    cJSON_AddStringToObject(msg, "v", "1");
    cJSON_AddStringToObject(msg, "t", "speak_done");
    cJSON_AddStringToObject(msg, "request_id", speech->request_id);
    cJSON_AddNumberToObject(msg, "question_seq", speech->question_seq);
    cJSON_AddNumberToObject(msg, "audio_id", speech->audio_id);
    cJSON_AddBoolToObject(msg, "ok", ok);
    char *json = cJSON_PrintUnformatted(msg);
    esp_err_t err = json ? net_send_json(json) : ESP_ERR_NO_MEM;
    ESP_LOGI(TAG, "question playback done: %s seq=%lu ok=%d ack=%s", speech->request_id,
             (unsigned long)speech->question_seq, ok, esp_err_to_name(err));
    free(json);
    cJSON_Delete(msg);
}

static void on_tts_audio(uint8_t *pcm, size_t len, uint32_t generation, const net_speech_t *speech)
{
    xSemaphoreTake(s_state_lock, portMAX_DELAY);
    tts_item_t item = {.pcm = pcm, .len = len, .play_generation = audio_play_generation(),
                       .speech_generation = generation, .speech = *speech};
    bool valid = generation == net_speech_generation() && !s_ptt_recording;
    bool queued = valid && s_tts_q && xQueueSend(s_tts_q, &item, 0) == pdTRUE;
    if (!queued) free(pcm);
    xSemaphoreGive(s_state_lock);
    if (valid && !queued) speech_done(speech, false);
}

static void tts_task(void *arg)
{
    (void)arg;
    tts_item_t item;
    while (1) {
        if (xQueueReceive(s_tts_q, &item, portMAX_DELAY) != pdTRUE) continue;
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        bool valid = item.play_generation == audio_play_generation() &&
                     item.speech_generation == net_speech_generation() && !s_ptt_recording;
        /* Questions animate speaking; terminal results remain visible during result audio. */
        bool question = *item.speech.request_id && s_task_active && s_question_waiting &&
                        !s_cancel_sent && !strcmp(item.speech.request_id, s_task_id) &&
                        item.speech.question_seq == s_question_seq;
        if (*item.speech.request_id && !question) valid = false;
        bool show_speaking = valid && (question || (!s_task_active &&
                              (s_state == UI_IDLE || s_state == UI_SPEAKING)));
        if (show_speaking) ui_set_state(UI_SPEAKING);
        uint32_t revision = s_ui_revision;
        xSemaphoreGive(s_state_lock);
        esp_err_t played = valid ? audio_play_current(item.pcm, item.len, item.play_generation) :
                                  ESP_ERR_INVALID_STATE;
        free(item.pcm);
        xSemaphoreTake(s_state_lock, portMAX_DELAY);
        bool acknowledge = valid && question && s_question_waiting && !s_cancel_sent &&
                           !strcmp(item.speech.request_id, s_task_id) && item.speech.question_seq == s_question_seq &&
                           item.play_generation == audio_play_generation() &&
                           item.speech_generation == net_speech_generation();
        if (show_speaking && revision == s_ui_revision &&
            item.play_generation == audio_play_generation() && !s_task_active && !s_ptt_busy) {
            ui_set_state(UI_IDLE);
        }
        xSemaphoreGive(s_state_lock);
        if (acknowledge) speech_done(&item.speech, played == ESP_OK);
    }
}

/* ------------------------------------------------------------------ LVGL */
static void player_tick_cb(lv_timer_t *t)
{
    (void)t;
    bluebot_player_tick(&s_pet, lv_tick_get());
}

void app_main(void)
{
    /* WiFi 驱动依赖 NVS */
    esp_err_t nvs_ret = nvs_flash_init();
    if (nvs_ret == ESP_ERR_NVS_NO_FREE_PAGES || nvs_ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        nvs_ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(nvs_ret);

    ESP_LOGI(TAG, "heap@start: internal=%uK psram=%uK",
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));

    bsp_display_start();
    bsp_display_brightness_set(100);      /* 默认全亮 */
    bsp_display_lock(-1);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0xF2F4F8), 0);
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    /* 屏幕不再触发录音：只保留物理键 + 语音唤醒（用户要求） */

    s_net_label = lv_label_create(scr);
    lv_obj_set_style_text_font(s_net_label, &dsbot_font_32, 0);
    lv_obj_set_style_text_color(s_net_label, lv_color_hex(0x8894A8), 0);
    lv_label_set_text(s_net_label, "连接中…");
    lv_obj_align(s_net_label, LV_ALIGN_TOP_MID, 0, 18);

    s_face = lv_image_create(scr);
    lv_image_set_src(s_face, &bluebot_idle_01);
    lv_obj_center(s_face);
    lv_obj_set_y(s_face, -20);

    s_label = lv_label_create(scr);
    lv_obj_set_style_text_font(s_label, &dsbot_font_32, 0);
    lv_obj_set_style_text_color(s_label, lv_color_hex(0x2B3A55), 0);
    lv_label_set_text(s_label, k_text[UI_IDLE]);
    lv_obj_align(s_label, LV_ALIGN_BOTTOM_MID, 0, -36);

    bluebot_player_bind(&s_pet, s_face, lv_tick_get());
    bluebot_player_set_state(&s_pet, BLUEBOT_IDLE, lv_tick_get());
    lv_timer_create(player_tick_cb, 30, NULL);

    bsp_display_unlock();

    s_state_lock = xSemaphoreCreateMutex();
    s_ptt_q = xQueueCreate(8, sizeof(ptt_evt_t));
    s_tts_q = xQueueCreate(4, sizeof(tts_item_t));
    assert(s_state_lock && s_ptt_q && s_tts_q);
    input_policy_init(&s_input);
    s_boot_id = esp_random();
    net_set_tts_cb(on_tts_audio);

    if (audio_init() == ESP_OK) {
        audio_play_tone(880, 150);
        ESP_LOGI(TAG, "audio self-test tone played");
        audio_wake_start(on_wake_clip);          /* 必须在 audio_init 之后 */
    } else {
        ESP_LOGE(TAG, "audio_init failed");
    }

    ESP_ERROR_CHECK(xTaskCreate(ptt_task, "dsbot_ptt", 4096, NULL, 6, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(xTaskCreate(tts_task, "dsbot_tts", 4096, NULL, 6, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(xTaskCreate(cancel_task, "dsbot_cancel", 4096, NULL, 6, NULL) == pdPASS ? ESP_OK : ESP_ERR_NO_MEM);
    ESP_ERROR_CHECK(buttons_init(on_button));
    axp2101_init(on_pwrkey);

    ESP_LOGI(TAG, "U-Face ready (%dx%d)", BLUEBOT_WIDTH, BLUEBOT_HEIGHT);
    ESP_LOGI(TAG, "heap@ui: internal=%uK psram=%uK",
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
             (unsigned)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));

    /* 联网（失败也继续跑，本地回放仍可用） */
    if (net_start(on_net_msg) == ESP_OK) {
        ui_set_net_locked("WiFi 已连，等待主机…");
    } else {
        ui_set_net_locked("WiFi 未连接");
    }

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(2000));
        if (!net_connected()) {
            ui_set_net_locked(net_connected() ? "已连接主机" : "离线");
        }
    }
}
