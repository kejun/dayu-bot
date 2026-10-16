/*
 * ds-bot 网络：WiFi STA + WebSocket 客户端（终端协议 v1）
 *
 * 与主机 `bridge serve` 通信：见 docs/protocol-v1.md
 *   - 控制消息：JSON 文本帧（hello / audio_start / audio_end / cancel / ping…）
 *   - 音频数据：二进制帧（PCM 16kHz 单声道 int16）
 */
#pragma once

#include <stddef.h>
#include <stdbool.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 收到主机消息时的回调（在 WebSocket 任务上下文，不要直接调 LVGL） */
typedef void (*net_msg_cb_t)(const char *type, const char *json);

esp_err_t net_start(net_msg_cb_t cb);
bool      net_connected(void);

/* 发送 JSON 文本帧 */
esp_err_t net_send_json(const char *json);
/* 发送音频二进制帧 */
esp_err_t net_send_binary(const void *data, size_t len);

#ifdef __cplusplus
}
#endif
