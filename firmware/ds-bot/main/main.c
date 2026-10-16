/*
 * ds-bot — Mini Agent Assistant 的 ESP32 终端界面
 *
 * 屏幕：Waveshare ESP32-S3-Touch-AMOLED-2.16（480×480 CO5300 QSPI + CST9220 触摸）
 * UI  ：LVGL 9 + U-Face 精灵素材（6 组表情 / 18 帧，128px RGB565A8）
 *
 * 本文件只负责「显示」：把 PRD §5.1 的终端状态映射成精灵表情。
 * 语音、联网、任务逻辑在后续接入，不在这里实现。
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

static void player_tick_cb(lv_timer_t *t)
{
    (void)t;
    bluebot_player_tick(&s_pet, lv_tick_get());
}

/* 切换界面状态。调用方需已持有 LVGL 锁。 */
static void ui_set_state(ui_state_t st)
{
    bluebot_player_set_state(&s_pet, k_face[st], lv_tick_get());
    lv_label_set_text(s_label, k_text[st]);
}

void app_main(void)
{
    bsp_display_start();
    bsp_display_lock(-1);

    lv_obj_t *scr = lv_screen_active();
    lv_obj_set_style_bg_color(scr, lv_color_hex(0xF2F4F8), 0);   /* 浅色背景，深蓝眼睛更清楚 */
    lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

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
    ESP_LOGI(TAG, "U-Face ready (%dx%d)", BLUEBOT_WIDTH, BLUEBOT_HEIGHT);

    /* 演示：循环走一遍 6 个状态；后续由语音/任务状态驱动 */
    static const ui_state_t seq[] = {UI_IDLE, UI_RECORDING, UI_PROCESSING,
                                     UI_SPEAKING, UI_OK, UI_FAIL};
    size_t i = 0;
    while (1) {
        vTaskDelay(pdMS_TO_TICKS(4000));
        i = (i + 1) % (sizeof(seq) / sizeof(seq[0]));
        bsp_display_lock(-1);
        ui_set_state(seq[i]);
        bsp_display_unlock();
        ESP_LOGI(TAG, "state -> %s", k_text[seq[i]]);
    }
}
