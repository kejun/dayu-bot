/*
 * ds-bot — Mini Agent Assistant 的 ESP32 终端界面
 *
 * 屏幕：Waveshare ESP32-S3-Touch-AMOLED-2.16（480×480 CO5300 QSPI + CST9220 触摸）
 * UI  ：LVGL 9 + U-Face 精灵素材（6 组表情 / 18 帧，256px RGB565A8）
 *
 * 当前阶段：验证触摸。点屏幕切换表情（BSP 已把触摸注册为 LVGL 输入设备）。
 * 语音、联网、任务逻辑在后续接入。
 */
#include <stddef.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "lvgl.h"
#include "bsp/esp-bsp.h"
#include "bsp/display.h"

#include "bluebot_assets.h"
#include "bluebot_player.h"
#include "dsbot_font_32.h"
#include "audio.h"

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

static ui_state_t s_state = UI_IDLE;

static void player_tick_cb(lv_timer_t *t)
{
    (void)t;
    bluebot_player_tick(&s_pet, lv_tick_get());
}

/* 切换界面状态。调用方需已持有 LVGL 锁。 */
static void ui_set_state(ui_state_t st)
{
    s_state = st;
    bluebot_player_set_state(&s_pet, k_face[st], lv_tick_get());
    lv_label_set_text(s_label, k_text[st]);
}

/* 触摸：按住录音，松手回放（验证 ES7210 麦克风 + ES8311 扬声器） */
static void on_screen_event(lv_event_t *e)
{
    lv_event_code_t code = lv_event_get_code(e);

    if (code == LV_EVENT_PRESSED) {
        if (audio_record_start() == ESP_OK) {
            ui_set_state(UI_RECORDING);
            ESP_LOGI(TAG, "pressed -> recording");
        }
    } else if (code == LV_EVENT_RELEASED) {
        size_t n = audio_record_stop();
        /* 太短（<200ms）不播，避免误触 */
        if (n > AUDIO_RATE_HZ * 2 / 5) {
            ESP_LOGI(TAG, "released -> playing back %u bytes", (unsigned)n);
            ui_set_state(UI_SPEAKING);
            audio_play(audio_record_data(), n);
        } else {
            ESP_LOGI(TAG, "released -> too short (%u bytes), skip", (unsigned)n);
        }
        ui_set_state(UI_IDLE);
    }
}

void app_main(void)
{
    bsp_display_start();
    bsp_display_lock(-1);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0xF2F4F8), 0);   /* 浅色背景，深蓝眼睛更清楚 */
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
    ui_set_state(UI_IDLE);
    lv_timer_create(player_tick_cb, 30, NULL);

    bsp_display_unlock();

    lv_indev_t *indev = bsp_display_get_input_dev();
    ESP_LOGI(TAG, "U-Face ready (%dx%d), touch indev=%p", BLUEBOT_WIDTH, BLUEBOT_HEIGHT, (void *)indev);

    /* 音频自检：先响一声，确认扬声器通路 */
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
