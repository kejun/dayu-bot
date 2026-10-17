/*
 * ds-bot 音频：ES7210 麦克风采集 + ES8311 扬声器播放
 *
 * 采样格式固定 16kHz / 单声道 / 16bit，与 docs/protocol-v1.md 的终端协议一致。
 * 录制缓冲放在 PSRAM。
 */
#pragma once

#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AUDIO_RATE_HZ   16000
#define AUDIO_MAX_SEC   10          /* 单次录音上限（PRD §5.1 要求 ≤60s，这里先给 10s） */

esp_err_t audio_init(void);

/* 开始录音（非阻塞，后台任务采集到 PSRAM 缓冲） */
esp_err_t audio_record_start(void);
/* 结束录音，返回本次录到的字节数 */
size_t    audio_record_stop(void);
/* 录音缓冲指针与长度 */
const void *audio_record_data(void);
size_t      audio_record_size(void);

/* 播放一段 PCM */
esp_err_t audio_play(const void *data, size_t len);
/* stop_speak 使在途及已排队的旧 generation 播放失效。 */
uint32_t audio_play_generation(void);
void audio_play_cancel(void);
esp_err_t audio_play_current(const void *data, size_t len, uint32_t generation);

/* 播放一段固定频率的正弦音（自检用，不需要麦克风） */
esp_err_t audio_play_tone(int freq_hz, int ms);

/* ---- 唤醒词：空闲时做 VAD 采集 ---- */
/* 检出一段有语音的片段时回调（在采集任务上下文，**不要阻塞太久**） */
typedef void (*audio_clip_cb_t)(const void *pcm, size_t len);

/* 启动空闲监听（VAD） */
esp_err_t audio_wake_start(audio_clip_cb_t cb);
/* 手动暂停/恢复（PTT 从按下到上传结束）；录音/播放期间另有内部互斥。 */
void audio_wake_pause(bool pause);

#ifdef __cplusplus
}
#endif
