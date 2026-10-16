/*
 * ds-bot 物理按键
 *
 * Waveshare ESP32-S3-Touch-AMOLED-2.16 的 KEYS 区（见原理图）：
 *   Key2 -> GPIO0    (BOOT)
 *   Key3 -> GPIO18
 *   Key1 -> PWRON    (中间那个键；接 AXP2101 电源管理，不是 GPIO，读它要过 I2C)
 * 两个可用键都接了外部 10K 上拉，按下为低电平。
 *
 * 实测（2026-10-16）：GPIO0 与 GPIO18 均正常触发；中间键 = PWRON，
 * 按用户选择保留其电源功能，不做「按住说话」。
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define BTN_GPIO_KEY3   18   /* Key3 */
#define BTN_GPIO_KEY2    0   /* Key2 (BOOT) */

/* 按键事件回调：gpio 为触发脚，pressed=true 表示按下 */
typedef void (*btn_event_cb_t)(int gpio, bool pressed);

/* 初始化按键并启动扫描任务（10ms 轮询 + 去抖） */
esp_err_t buttons_init(btn_event_cb_t cb);

#ifdef __cplusplus
}
#endif
