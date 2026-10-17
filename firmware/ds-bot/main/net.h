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
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* 收到主机消息时的回调（在 WebSocket 任务上下文，不要直接调 LVGL） */
typedef void (*net_msg_cb_t)(const char *type, const char *json);

/* 收到一整段主机语音（TTS）时回调；**回调负责释放 pcm** */
typedef struct {
    uint32_t audio_id, question_seq;
    char request_id[96];
} net_speech_t;
typedef void (*net_tts_cb_t)(uint8_t *pcm, size_t len, uint32_t generation,
                             const net_speech_t *speech);

void net_set_tts_cb(net_tts_cb_t cb);

esp_err_t net_start(net_msg_cb_t cb);
bool      net_connected(void);

/* 发送 JSON 文本帧 */
esp_err_t net_send_json(const char *json);
/* 发送音频二进制帧 */
esp_err_t net_send_binary(const void *data, size_t len);
/* 录音起止帧与 PCM 必须作为一个事务发送，避免 PTT / VAD 交错。 */
esp_err_t net_send_audio_clip(const char *request_id, const char *mode,
                              const void *data, size_t len);
esp_err_t net_send_answer_clip(const char *clip_id, const char *request_id,
                               uint32_t question_seq, const void *data, size_t len);
void net_cancel_audio_uploads(void);
/* 可从任意任务调用；令尚未接收完的旧 TTS 失效。 */
void net_discard_speech(void);
uint32_t net_speech_generation(void);

#ifdef __cplusplus
}
#endif
