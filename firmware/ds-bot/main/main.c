/*
 * ds-bot — Mini Agent Assistant 的 ESP32 终端界面
 *
 * 屏幕：Waveshare ESP32-S3-Touch-AMOLED-2.16（480×480 CO5300 QSPI + CST9220 触摸）
 * UI  ：LVGL 9 + U-Face 精灵素材（6 组表情 / 18 帧，256px RGB565A8）
 * 音频：ES7210 麦克风 + ES8311 扬声器（16kHz 单声道 16bit）
 * 按键：Key3=GPIO18（中间键）、Key2=GPIO0（BOOT）
 *
 * 「按住说话」由触摸和物理键共用一条 PTT 通路：
 *   按下 -> 录音（精灵=录音中…）；松开 -> 回放（精灵=播报中…）
 * 语音上传/联网在后续接入。
 */
#include <stddef.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "lvgl.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"

#include "bluebot_assets.h"
#include "bluebot_player.h"
#include "dsbot_font_32.h"
#include "audio.h"
#include "buttons.h"

static const char *TAG = "ds-bot";

static bluebot_player_t s_pet;
static lv_obj_t *s_face;
static lv_obj_t *s_label;

/* 终端状态（PRD §5.1：待机/录音/处理中/需要用户/结果） */
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

/* ---- PTT（按住说话）：触摸和物理键都投递到这里 ---- */
typedef enum { PTT_PRESS, PTT_RELEASE } ptt_evt_t;
static QueueHandle_t s_ptt_q;

static void ptt_post(ptt_evt_t e)
{
    if (s_ptt_q) {
        xQueueSend(s_ptt_q, &e, 0);
    }
}

static void ui_set_state_locked(ui_state_t st)
{
    bsp_display_lock(-1);
    bluebot_player_set_state(&s_pet, k_face[st], lv_tick_get());
    lv_label_set_text(s_label, k_text[st]);
    bsp_display_unlock();
}

static void ptt_task(void *arg)
{
    (void)arg;
    ptt_evt_t e;
    while (1) {
        if (xQueueReceive(s_ptt_q, &e, portMAX_DELAY) != pdTRUE) {
            continue;
        }
        if (e == PTT_PRESS) {
            if (audio_record_start() == ESP_OK) {
                ui_set_state_locked(UI_RECORDING);
                ESP_LOGI(TAG, "PTT: recording");
            }
        } else {
            size_t n = audio_record_stop();
            if (n > AUDIO_RATE_HZ * 2 / 5) {          /* >200ms 才回放 */
                ESP_LOGI(TAG, "PTT: playback %u bytes", (unsigned)n);
                ui_set_state_locked(UI_SPEAKING);
                audio_play(audio_record_data(), n);
            } else {
                ESP_LOGI(TAG, "PTT: too short (%u bytes)", (unsigned)n);
            }
            ui_set_state_locked(UI_IDLE);
        }
    }
}

/* ---- 事件源 ---- */
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

/* ---- LVGL ---- */
static void player_tick_cb(lv_timer_t *t)
{
    (void)t;
    bluebot_player_tick(&s_pet, lv_tick_get());
}

void app_main(void)
{
    bsp_display_start();
    bsp_display_lock(-1);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0xF2F4F8), 0);   /* 浅色背景 */
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
    lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
    lv_obj_add_event_cb(scr, on_screen_event, LV_EVENT_PRESSED, NULL);
    lv_obj_add_event_cb(scr, on_screen_event, LV_EVENT_RELEASED, NULL);

    s_face = lv_image_create(scr);
    lv_image_set_src(s_face, &bluebot_idle_01);
    lv_obj_center(s_face);
    lv_obj_set_y(s_face, -28);

    s_label = lv_label_create(scr);
    lv_obj_set_style_text_font(s_label, &dsbot_font_32, 0);   /* 32px 中文字体 */
    lv_obj_set_style_text_color(s_label, lv_color_hex(0x2B3A55), 0);
    lv_obj_align(s_label, LV_ALIGN_BOTTOM_MID, 0, -36);

    bluebot_player_bind(&s_pet, s_face, lv_tick_get());
    bluebot_player_set_state(&s_pet, BLUEBOT_IDLE, lv_tick_get());
    lv_label_set_text(s_label, k_text[UI_IDLE]);
    lv_timer_create(player_tick_cb, 30, NULL);

    bsp_display_unlock();

    /* PTT 队列 + 任务 */
    s_ptt_q = xQueueCreate(8, sizeof(ptt_evt_t));
    xTaskCreate(ptt_task, "dsbot_ptt", 4096, NULL, 6, NULL);

    /* 物理键 */
    buttons_init(on_button);

    ESP_LOGI(TAG, "U-Face ready (%dx%d), touch indev=%p",
             BLUEBOT_WIDTH, BLUEBOT_HEIGHT, (void *)bsp_display_get_input_dev());

    /* 音频自检 */
    if (audio_init() == ESP_OK) {
        audio_play_tone(880, 150);
        ESP_LOGI(TAG, "audio self-test tone played");
    } else {
        ESP_LOGE(TAG, "audio_init failed");
    }

    while (1) {
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
}
