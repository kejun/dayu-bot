#include "audio.h"

#include <string.h>
#include <math.h>
#include <stdatomic.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
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
static atomic_bool s_recording;
static volatile size_t s_rec_len;
static TaskHandle_t s_rec_task;
static SemaphoreHandle_t s_mic_lock;
static SemaphoreHandle_t s_play_lock;
static SemaphoreHandle_t s_rec_done;
static atomic_uint s_play_generation;
static atomic_bool s_playing;

static void rec_task(void *arg)
{
    (void)arg;
    const size_t chunk = 2048;
    size_t total = 0;
    xSemaphoreTake(s_mic_lock, portMAX_DELAY);
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
    xSemaphoreGive(s_mic_lock);

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
    xSemaphoreGive(s_rec_done);
    vTaskDelete(NULL);
}

esp_err_t audio_init(void)
{
    if (s_spk && s_mic && s_buf && s_mic_lock && s_play_lock && s_rec_done) {
        return ESP_OK;
    }

    s_mic_lock = xSemaphoreCreateMutex();
    s_play_lock = xSemaphoreCreateMutex();
    s_rec_done = xSemaphoreCreateBinary();
    if (!s_mic_lock || !s_play_lock || !s_rec_done) return ESP_ERR_NO_MEM;
    xSemaphoreGive(s_rec_done);
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
    esp_codec_dev_set_in_gain(s_mic, 30.0f);

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
    if (!s_mic || !s_buf || !s_rec_done) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_recording) {
        return ESP_OK;
    }
    /* PTT 已使旧播放失效；等待最后一次 codec write 返回再开始采集。 */
    xSemaphoreTake(s_play_lock, portMAX_DELAY);
    xSemaphoreGive(s_play_lock);
    if (xSemaphoreTake(s_rec_done, 0) != pdTRUE) return ESP_ERR_INVALID_STATE;
    s_rec_len = 0;
    s_recording = true;
    if (xTaskCreate(rec_task, "dsbot_rec", 4096, NULL, 6, &s_rec_task) != pdPASS) {
        s_recording = false;
        xSemaphoreGive(s_rec_done);
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

size_t audio_record_stop(void)
{
    if (!s_rec_done) return 0;
    s_recording = false;
    /* codec read 超时可达 1 秒；必须收到任务完成，才能复用缓冲。 */
    xSemaphoreTake(s_rec_done, portMAX_DELAY);
    xSemaphoreGive(s_rec_done);
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
    return audio_play_current(data, len, audio_play_generation());
}

uint32_t audio_play_generation(void)
{
    return atomic_load(&s_play_generation);
}

void audio_play_cancel(void)
{
    atomic_fetch_add(&s_play_generation, 1);
}

esp_err_t audio_play_current(const void *data, size_t len, uint32_t generation)
{
    if (!s_spk || !s_play_lock || !data || !len) {
        return ESP_ERR_INVALID_ARG;
    }
    xSemaphoreTake(s_play_lock, portMAX_DELAY);
    if (generation != audio_play_generation()) {
        xSemaphoreGive(s_play_lock);
        return ESP_ERR_INVALID_STATE;
    }
    s_playing = true;
    esp_err_t result = ESP_OK;
    /* 软件增益：硬件已拉满，但源音频（麦克风回放/TTS）电平往往偏低 */
    const float gain = 2.0f;
    int16_t stage[AUDIO_RATE_HZ / 50]; /* 20ms 边界检查取消 */
    const int16_t *src = (const int16_t *)data;
    size_t total = len / 2;
    size_t done = 0;
    while (done < total) {
        if (generation != audio_play_generation()) {
            result = ESP_ERR_INVALID_STATE;
            break;
        }
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
            result = ESP_FAIL;
            break;
        }
        done += n;
    }
    /* BSP uses 6 DMA descriptors × 240 frames = 90ms at 16kHz. Writing 120ms
     * of silence drains the last speech samples before a playback receipt. */
    memset(stage, 0, sizeof(stage));
    for (int tail = 0; result == ESP_OK && tail < 6; ++tail) {
        if (generation != audio_play_generation()) result = ESP_ERR_INVALID_STATE;
        else if (esp_codec_dev_write(s_spk, stage, sizeof(stage)) != ESP_CODEC_DEV_OK) result = ESP_FAIL;
    }
    s_playing = false;
    xSemaphoreGive(s_play_lock);
    return result;
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
#define WAKE_MAX_MS     5000      /* 容纳完整“小D小D，取消任务”，避免慢速口令被截成半句 */
#define WAKE_SIL_MS     900
#define WAKE_RMS_ON     1200      /* 起录阈值 */
#define WAKE_RMS_OFF    600       /* 静音判定阈值 */
#define WAKE_FRAME_BYTES (AUDIO_RATE_HZ * BYTES_PER_SAMPLE * WAKE_FRAME_MS / 1000)
#define WAKE_MAX_BYTES   (AUDIO_RATE_HZ * BYTES_PER_SAMPLE * WAKE_MAX_MS / 1000)

static audio_clip_cb_t s_clip_cb;
static atomic_bool s_wake_paused = true;
static atomic_uint s_wake_generation;
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
    int max_rms = 0;            /* 诊断：每秒打印最大 RMS，便于标定阈值 */
    int diag_ms = 0;
    uint32_t generation = atomic_load(&s_wake_generation);

    while (1) {
        if (s_recording || !s_wake_buf) {
            vTaskDelay(pdMS_TO_TICKS(50));
            collecting = false;
            continue;
        }
        xSemaphoreTake(s_mic_lock, portMAX_DELAY);
        if (s_recording) {
            xSemaphoreGive(s_mic_lock);
            collecting = false;
            continue;
        }
        int ret = esp_codec_dev_read(s_mic, frame, fbytes);
        xSemaphoreGive(s_mic_lock);
        /* Keep draining RX during speech so reopening cannot capture buffered echo. */
        if (s_wake_paused || s_recording || s_playing ||
            generation != atomic_load(&s_wake_generation)) {
            generation = atomic_load(&s_wake_generation);
            collecting = false;
            continue;
        }
        if (ret != ESP_CODEC_DEV_OK) {
            vTaskDelay(pdMS_TO_TICKS(20));
            continue;
        }
        int rms = frame_rms(frame, fbytes / 2);
        if (rms > max_rms) max_rms = rms;
        if ((diag_ms += WAKE_FRAME_MS) >= 2000) {
            ESP_LOGI(TAG, "wake: 2s 内最大 rms=%d (阈值 %d)", max_rms, WAKE_RMS_ON);
            max_rms = 0; diag_ms = 0;
        }

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
    if (pause) atomic_fetch_add(&s_wake_generation, 1);
    s_wake_paused = pause;
}
