/*
 * ds-bot 极简 AXP2101 驱动 —— 只做一件事：检测 PWRKEY（中间键）短按。
 *
 * 中间那个物理键（丝印 Key1）接的是 AXP2101 的 PWRON，不是 ESP32 的 GPIO，
 * 所以只能过 I2C 读它的中断状态。
 *
 * 寄存器（见 XPowersLib/AXP2101Constants.h）：
 *   INTEN2  (0x41)  bit3 = PWRKEY 短按中断使能
 *   INTSTS2 (0x49)  bit3 = PWRKEY 短按中断状态（写 1 清除）
 *
 * 注意：长按 PWRON 仍会关机——那是 PMIC 自己的行为，改不了。
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AXP2101_ADDR 0x34

/* PWRKEY 短按回调（在轮询任务上下文） */
typedef void (*axp_pwrkey_cb_t)(void);

/* 初始化（复用 BSP 的 I2C 总线）并使能 PWRKEY 短按中断 */
esp_err_t axp2101_init(axp_pwrkey_cb_t cb);

/* 读芯片 ID（自检用；AXP2101 应为 0x4A） */
esp_err_t axp2101_chip_id(uint8_t *id);

#ifdef __cplusplus
}
#endif
