/*
 * ds-bot — Mini Agent Assistant 的 ESP32 终端界面
 *
 * 屏幕：Waveshare ESP32-S3-Touch-AMOLED-2.16（480×480 CO5300 QSPI + CST9220 触摸）
 * UI  ：LVGL 9 + U-Face 精灵素材（6 组表情 / 18 帧，256px RGB565A8）
 * 音频：ES7210 麦克风 + ES8311 扬声器（16kHz 单声道 16bit）
 * 按键：Key3=GPIO18、Key2=GPIO0（中间键 PWRON 保留电源功能）
 * 网络：WiFi → 主机 bridge serve（WebSocket + 终端协议 v1）
 *
 * 闭环：按住说话 → 录 16kHz PCM → 上传主机 → 主机 STT/Agent/iCTRL →
 *       状态/追问/结果回传 → 精灵表情 + 语音播报
 */
#include <stddef.h>
#include <stdio.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
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
#include "net.h"
#include "axp2101.h"

static const char *TAG = "ds-bot";

static bluebot_player_t s_pet;
static lv_obj_t *s_face;
static lv_obj_t *s_label;
static lv_obj_t *s_net_label;

/* 终端状态（PRD §5.1） */
typedef enum {
    UI_IDLE,        /* 待机 */
    UI_RECORDING,   /* 录音中 */
    UI_PROCESSING,  /* 处理中 */
    UI_SPEAKING,    /* 播报中 */
    UI_OK,          /* 成功 */
    UI_FAIL,        /* 失败 / 结果未确认 */
    UI_STATE_COUNT
} ui_state_t;

static const bluebot_state_t k_face[] = {
    [UI_IDLE] = BLUEBOT_IDLE,
    [UI_RECORDING] = BLUEBOT_LISTEN,
    [UI_PROCESSING] = BLUEBOT_THINK,
    [UI_SPEAKING] = BLUEBOT_SPEAK,
    [UI_OK] = BLUEBOT_HAPPY,
    [UI_FAIL] = BLUEBOT_SAD,
};

static const char *k_text[] = {
    [UI_IDLE] = "待机",
    [UI_RECORDING] = "录音中…",
    [UI_PROCESSING] = "处理中…",
    [UI_SPEAKING] = "播报中…",
    [UI_OK] = "完成",
    [UI_FAIL] = "失败",
};

static ui_state_t s_state = UI_IDLE;
static bool s_screen_on = true;

/* 中间键（PWRON，走 AXP2101）短按：开/关屏幕 */
static void on_pwrkey(void)
{
    s_screen_on = !s_screen_on;
    bsp_display_brightness_set(s_screen_on ? 100 : 0);
    ESP_LOGI(TAG, "screen %s", s_screen_on ? "ON" : "OFF");
}

/* ---------------------------------------------------------------- UI 更新 */
static void ui_set_state_locked(ui_state_t st)
{
    s_state = st;
    bsp_display_lock(-1);
    bluebot_player_set_state(&s_pet, k_face[st], lv_tick_get());
    lv_label_set_text(s_label, k_text[st]);
    bsp_display_unlock();
}

static void ui_set_net_locked(const char *text)
{
    bsp_display_lock(-1);
    lv_label_set_text(s_net_label, text);
    bsp_display_unlock();
}

/* ------------------------------------------------------- 主机消息 → 精灵 */
static ui_state_t state_from_name(const char *s)
{
    if (!s) return UI_IDLE;
    if (!strcmp(s, "recording"))  return UI_RECORDING;
    if (!strcmp(s, "processing")) return UI_PROCESSING;
    if (!strcmp(s, "need_user"))  return UI_PROCESSING;   /* 追问：先复用「思考」表情 */
    return UI_IDLE;
}

static void on_net_msg(const char *type, const char *json)
{
    cJSON *root = cJSON_Parse(json);
    if (!root) {
        return;
    }
    cJSON *t = cJSON_GetObjectItem(root, "t");
    const char *tt = cJSON_IsString(t) ? t->valuestring : type;

    if (!strcmp(tt, "welcome")) {
        ui_set_net_locked("已连接主机");
    } else if (!strcmp(tt, "notice")) {
        cJSON *txt = cJSON_GetObjectItem(root, "text");
        cJSON *lvl = cJSON_GetObjectItem(root, "level");
        const char *lv = cJSON_IsString(lvl) ? lvl->valuestring : "warn";
        ESP_LOGW(TAG, "notice[%s]: %s", lv, cJSON_IsString(txt) ? txt->valuestring : "");
        /* 出错时别一直卡在「处理中」 */
        if (!strcmp(lv, "error")) {
            ui_set_state_locked(UI_FAIL);
        }
    } else if (!strcmp(tt, "status")) {
        cJSON *st = cJSON_GetObjectItem(root, "state");
        ui_set_state_locked(state_from_name(cJSON_IsString(st) ? st->valuestring : NULL));
    } else if (!strcmp(tt, "question")) {
        cJSON *txt = cJSON_GetObjectItem(root, "text");
        ESP_LOGI(TAG, "question: %s", cJSON_IsString(txt) ? txt->valuestring : "");
        ui_set_state_locked(UI_PROCESSING);
    } else if (!strcmp(tt, "result")) {
        cJSON *kind = cJSON_GetObjectItem(root, "kind");
        cJSON *txt  = cJSON_GetObjectItem(root, "text");
        const char *k = cJSON_IsString(kind) ? kind->valuestring : "unknown";
        ESP_LOGI(TAG, "result[%s]: %s", k, cJSON_IsString(txt) ? txt->valuestring : "");
        ui_set_state_locked(!strcmp(k, "success") ? UI_OK : UI_FAIL);
    }
    cJSON_Delete(root);
}

/* ------------------------------------------------------------------- PTT */
typedef enum { PTT_PRESS, PTT_RELEASE } ptt_evt_t;
static QueueHandle_t s_ptt_q;
static uint32_t s_req_seq;

static void ptt_post(ptt_evt_t e)
{
    if (s_ptt_q) {
        xQueueSend(s_ptt_q, &e, 0);
    }
}

static void ptt_task(void *arg)
{
    (void)arg;
    ptt_evt_t e;
    char rid[24];
    while (1) {
        if (xQueueReceive(s_ptt_q, &e, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (e == PTT_PRESS) {
            snprintf(rid, sizeof(rid), "r%u", ++s_req_seq);
            if (audio_record_start() == ESP_OK) {
                ui_set_state_locked(UI_RECORDING);
                if (net_connected()) {
                    char msg[128];
                    snprintf(msg, sizeof(msg),
                             "{\"v\":\"1\",\"t\":\"audio_start\",\"request_id\":\"%s\",\"seq\":0}", rid);
                    net_send_json(msg);
                } else {
                    ESP_LOGW(TAG, "未连主机，仅本地回放");
                }
            }
        } else {
            size_t n = audio_record_stop();
            if (n > AUDIO_RATE_HZ * 2 / 5) {          /* >200ms */
                if (net_connected()) {
                    /* 上传：分块二进制帧 + audio_end */
                    const uint8_t *p = audio_record_data();
                    size_t off = 0;
                    while (off < n) {
                        size_t chunk = (n - off) > 4096 ? 4096 : (n - off);
                        net_send_binary(p + off, chunk);
                        off += chunk;
                    }
                    char msg[128];
                    snprintf(msg, sizeof(msg),
                             "{\"v\":\"1\",\"t\":\"audio_end\",\"request_id\":\"%s\",\"duration_ms\":%u}",
                             rid, (unsigned)(n * 1000 / (AUDIO_RATE_HZ * 2)));
                    net_send_json(msg);
                    ESP_LOGI(TAG, "uploaded %u bytes, rid=%s", (unsigned)n, rid);
                    ui_set_state_locked(UI_PROCESSING);
                } else {
                    ESP_LOGI(TAG, "本地回放 %u bytes", (unsigned)n);
                    ui_set_state_locked(UI_SPEAKING);
                    audio_play(audio_record_data(), n);
                    ui_set_state_locked(UI_IDLE);
                }
            } else {
                ESP_LOGI(TAG, "太短，忽略 (%u bytes)", (unsigned)n);
                if (net_connected()) {
                    char msg[96];
                    snprintf(msg, sizeof(msg),
                             "{\"v\":\"1\",\"t\":\"audio_abort\",\"request_id\":\"%s\"}", rid);
                    net_send_json(msg);
                }
                ui_set_state_locked(UI_IDLE);
            }
        }
    }
}

/* -------------------------------------------------------------- 事件源 */
static void on_screen_event(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);
    if (code == LV_EVENT_PRESSED) {
        ptt_post(PTT_PRESS);
    } else if (code == LV_EVENT_RELEASED) {
        ptt_post(PTT_RELEASE);
    }
}

static void on_button(int gpio, bool pressed)
{
    ESP_LOGI(TAG, "button GPIO%d %s", gpio, pressed ? "down" : "up");
    ptt_post(pressed ? PTT_PRESS : PTT_RELEASE);
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
    lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(scr, on_screen_event, LV_EVENT_PRESSED, NULL);
    lv_obj_add_event_cb(scr, on_screen_event, LV_EVENT_RELEASED, NULL);

    s_net_label = lv_label_create(scr);
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

    s_ptt_q = xQueueCreate(8, sizeof(ptt_evt_t));
    xTaskCreate(ptt_task, "dsbot_ptt", 4096, NULL, 6, NULL);
    buttons_init(on_button);
    axp2101_init(on_pwrkey);

    if (audio_init() == ESP_OK) {
        audio_play_tone(880, 150);
        ESP_LOGI(TAG, "audio self-test tone played");
    } else {
        ESP_LOGE(TAG, "audio_init failed");
    }

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
