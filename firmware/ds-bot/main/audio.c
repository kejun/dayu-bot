#include "audio.h"

#include <string.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_heap_caps.h"
#include "bsp/esp-bsp.h"
#include "esp_codec_dev.h"

static const char *TAG = "ds-bot-audio";

#define BYTES_PER_SAMPLE 2          /* 16bit 单声道 */
#define MAX_BYTES        (AUDIO_RATE_HZ * BYTES_PER_SAMPLE * AUDIO_MAX_SEC)

static esp_codec_dev_handle_t s_spk;
static esp_codec_dev_handle_t s_mic;

static uint8_t *s_buf;              /* PSRAM 录音缓冲 */
static volatile bool s_recording;
static volatile size_t s_rec_len;
static TaskHandle_t s_rec_task;

static void rec_task(void *arg)
{
    (void)arg;
    const size_t chunk = 2048;
    size_t total = 0;
    ESP_LOGI(TAG, "recording start");
    while (s_recording && (total + chunk) <= MAX_BYTES) {
        int ret = esp_codec_dev_read(s_mic, s_buf + total, chunk);
        if (ret == ESP_CODEC_DEV_OK) {
            total += chunk;
        } else {
            vTaskDelay(pdMS_TO_TICKS(2));
        }
    }
    s_rec_len = total;
    s_recording = false;

    /* 诊断：看麦克风到底录到了什么 */
    const int16_t *p = (const int16_t *)s_buf;
    size_t c = total / 2;
    int32_t mx = 0;
    int64_t sum = 0;
    for (size_t i = 0; i < c; i++) {
        int32_t v = p[i];
        if (v < 0) v = -v;
        if (v > mx) mx = v;
        sum += (int64_t)v * v;
    }
    int rms = c ? (int)sqrt((double)sum / (double)c) : 0;
    ESP_LOGI(TAG, "recording stop: %u bytes (%.2fs) peak=%d rms=%d",
             (unsigned)total, (double)total / (AUDIO_RATE_HZ * BYTES_PER_SAMPLE),
             (int)mx, rms);
    s_rec_task = NULL;
    vTaskDelete(NULL);
}

esp_err_t audio_init(void)
{
    if (s_spk && s_mic) {
        return ESP_OK;
    }

    s_spk = bsp_audio_codec_speaker_init();
    s_mic = bsp_audio_codec_microphone_init();
    if (!s_spk || !s_mic) {
        ESP_LOGE(TAG, "codec init failed (spk=%p mic=%p)", (void *)s_spk, (void *)s_mic);
        return ESP_FAIL;
    }

    esp_codec_dev_sample_info_t fs = {
        .bits_per_sample = 16,
        .channel = 1,
        .channel_mask = 0,
        .sample_rate = AUDIO_RATE_HZ,
        .mclk_multiple = 0,         /* 0 => mclk = rate * 256 */
    };
    ESP_ERROR_CHECK(esp_codec_dev_open(s_spk, &fs));
    ESP_ERROR_CHECK(esp_codec_dev_open(s_mic, &fs));
    esp_codec_dev_set_out_vol(s_spk, 100);   /* 音量曲线 100 = 0dB（满幅） */
    esp_codec_dev_set_in_gain(s_mic, 36.0f);

    if (!s_buf) {
        s_buf = heap_caps_malloc(MAX_BYTES, MALLOC_CAP_SPIRAM);
        if (!s_buf) {
            ESP_LOGE(TAG, "PSRAM alloc %u bytes failed", (unsigned)MAX_BYTES);
            return ESP_ERR_NO_MEM;
        }
    }
    ESP_LOGI(TAG, "audio ready: %d Hz mono 16bit, buffer %u KiB (PSRAM)",
             AUDIO_RATE_HZ, (unsigned)(MAX_BYTES / 1024));
    return ESP_OK;
}

esp_err_t audio_record_start(void)
{
    if (!s_mic) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_recording) {
        return ESP_OK;
    }
    s_rec_len = 0;
    s_recording = true;
    if (xTaskCreate(rec_task, "dsbot_rec", 4096, NULL, 6, &s_rec_task) != pdPASS) {
        s_recording = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

size_t audio_record_stop(void)
{
    if (!s_recording) {
        return s_rec_len;
    }
    s_recording = false;
    /* 等采集任务收尾（最多 ~200ms） */
    for (int i = 0; i < 100 && s_rec_task; i++) {
        vTaskDelay(pdMS_TO_TICKS(2));
    }
    return s_rec_len;
}

const void *audio_record_data(void)
{
    return s_buf;
}

size_t audio_record_size(void)
{
    return s_rec_len;
}

esp_err_t audio_play(const void *data, size_t len)
{
    if (!s_spk || !data || !len) {
        return ESP_ERR_INVALID_ARG;
    }
    /* 软件增益：硬件已拉满，但源音频（麦克风回放/TTS）电平往往偏低 */
    const float gain = 2.0f;
    static int16_t stage[2048];
    const int16_t *src = (const int16_t *)data;
    size_t total = len / 2;
    size_t done = 0;
    while (done < total) {
        size_t n = total - done;
        if (n > sizeof(stage) / sizeof(stage[0])) {
            n = sizeof(stage) / sizeof(stage[0]);
        }
        for (size_t i = 0; i < n; i++) {
            int32_t v = (int32_t)(src[done + i] * gain);
            if (v > 32767) v = 32767;
            if (v < -32768) v = -32768;
            stage[i] = (int16_t)v;
        }
        int ret = esp_codec_dev_write(s_spk, stage, (int)(n * sizeof(int16_t)));
        if (ret != ESP_CODEC_DEV_OK) {
            ESP_LOGE(TAG, "write failed: %d", ret);
            return ESP_FAIL;
        }
        done += n;
    }
    return ESP_OK;
}

esp_err_t audio_play_tone(int freq_hz, int ms)
{
    const int n = AUDIO_RATE_HZ * ms / 1000;
    int16_t *tone = heap_caps_malloc(n * sizeof(int16_t), MALLOC_CAP_SPIRAM);
    if (!tone) {
        return ESP_ERR_NO_MEM;
    }
    for (int i = 0; i < n; i++) {
        tone[i] = (int16_t)(sinf(2.0f * (float)M_PI * freq_hz * i / AUDIO_RATE_HZ) * 12000);
    }
    esp_err_t ret = audio_play(tone, n * sizeof(int16_t));
    free(tone);
    return ret;
}

/* ==================== 唤醒词：空闲 VAD 采集 ==================== */
/*
 * 空闲时读麦克风，按 20ms 帧算 RMS。超过阈值认为有语音，开始收集；
 * 连续静音 WAKE_SIL_MS 或收集满 WAKE_MAX_MS 就回调一段 PCM 给上层
 * （上层发给主机做识别，找「小弟」）。
 */
#define WAKE_FRAME_MS   20
#define WAKE_MAX_MS     2500
#define WAKE_SIL_MS     700
#define WAKE_RMS_ON     1400      /* 起录阈值 */
#define WAKE_RMS_OFF    700       /* 静音判定阈值 */
#define WAKE_FRAME_BYTES (AUDIO_RATE_HZ * BYTES_PER_SAMPLE * WAKE_FRAME_MS / 1000)
#define WAKE_MAX_BYTES   (AUDIO_RATE_HZ * BYTES_PER_SAMPLE * WAKE_MAX_MS / 1000)

static audio_clip_cb_t s_clip_cb;
static volatile bool   s_wake_paused = true;
static uint8_t *s_wake_buf;

static int frame_rms(const int16_t *p, size_t n)
{
    int64_t sum = 0;
    for (size_t i = 0; i < n; i++) {
        sum += (int64_t)p[i] * p[i];
    }
    return n ? (int)sqrt((double)sum / (double)n) : 0;
}

static void wake_task(void *arg)
{
    (void)arg;
    const size_t fbytes = WAKE_FRAME_BYTES;
    int16_t frame[WAKE_FRAME_BYTES / 2];
    size_t total = 0;
    int silence_ms = 0;
    bool collecting = false;

    while (1) {
        if (s_wake_paused || !s_wake_buf) {
            vTaskDelay(pdMS_TO_TICKS(50));
            collecting = false;
            continue;
        }
        int ret = esp_codec_dev_read(s_mic, frame, fbytes);
        if (ret != ESP_CODEC_DEV_OK) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        int rms = frame_rms(frame, fbytes / 2);

        if (!collecting) {
            if (rms > WAKE_RMS_ON) {
                collecting = true;
                total = 0;
                silence_ms = 0;
                ESP_LOGI(TAG, "wake: 检测到语音 (rms=%d)", rms);
            } else {
                continue;
            }
        }
        /* 收集 */
        if (total + fbytes <= WAKE_MAX_BYTES) {
            memcpy(s_wake_buf + total, frame, fbytes);
            total += fbytes;
        }
        silence_ms = (rms < WAKE_RMS_OFF) ? (silence_ms + WAKE_FRAME_MS) : 0;

        if (silence_ms >= WAKE_SIL_MS || total + fbytes > WAKE_MAX_BYTES) {
            collecting = false;
            if (total > (size_t)(AUDIO_RATE_HZ * BYTES_PER_SAMPLE / 2)) {   /* >0.5s */
                ESP_LOGI(TAG, "wake: 片段 %u 字节", (unsigned)total);
                if (s_clip_cb) {
                    s_clip_cb(s_wake_buf, total);
                }
            }
            total = 0;
        }
    }
}

esp_err_t audio_wake_start(audio_clip_cb_t cb)
{
    if (!s_mic) {
        return ESP_ERR_INVALID_STATE;
    }
    s_clip_cb = cb;
    if (!s_wake_buf) {
        s_wake_buf = heap_caps_malloc(WAKE_MAX_BYTES, MALLOC_CAP_SPIRAM);
        if (!s_wake_buf) {
            return ESP_ERR_NO_MEM;
        }
    }
    s_wake_paused = false;
    if (xTaskCreate(wake_task, "dsbot_wake", 4096, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "wake: 空闲 VAD 监听已启动 (阈值 rms>%d)", WAKE_RMS_ON);
    return ESP_OK;
}

void audio_wake_pause(bool pause)
{
    s_wake_paused = pause;
}
