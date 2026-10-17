#include "net.h"

#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_log.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_websocket_client.h"
#include "esp_heap_caps.h"
#include "cJSON.h"

static const char *TAG = "ds-bot-net";

#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1
#define WIFI_MAX_RETRY     8

static EventGroupHandle_t s_wifi_eg;
static int  s_retry;
static esp_websocket_client_handle_t s_ws;
static net_msg_cb_t s_msg_cb;
static net_tts_cb_t s_tts_cb;
static volatile bool s_ws_up;

/* 主机 TTS 音频接收缓冲（PSRAM） */
static uint8_t *s_tts_buf;
static size_t   s_tts_need;
static size_t   s_tts_got;

void net_set_tts_cb(net_tts_cb_t cb)
{
    s_tts_cb = cb;
}

/* ------------------------------------------------------------------ WiFi */
static void wifi_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    if (base == WIFI_EVENT && id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (base == WIFI_EVENT && id == WIFI_EVENT_STA_DISCONNECTED) {
        s_ws_up = false;
        if (s_retry++ < WIFI_MAX_RETRY) {
            ESP_LOGW(TAG, "wifi disconnected, retry %d", s_retry);
            esp_wifi_connect();
        } else {
            xEventGroupSetBits(s_wifi_eg, WIFI_FAIL_BIT);
        }
    } else if (base == IP_EVENT && id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *ev = (ip_event_got_ip_t *)data;
        ESP_LOGI(TAG, "got ip: " IPSTR, IP2STR(&ev->ip_info.ip));
        s_retry = 0;
        xEventGroupSetBits(s_wifi_eg, WIFI_CONNECTED_BIT);
    }
}

static esp_err_t wifi_start(void)
{
    s_wifi_eg = xEventGroupCreate();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t init_cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init_cfg));

    ESP_ERROR_CHECK(esp_event_handler_instance_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                        wifi_event_handler, NULL, NULL));
    ESP_ERROR_CHECK(esp_event_handler_instance_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                        wifi_event_handler, NULL, NULL));

    wifi_config_t wc = {0};
    strlcpy((char *)wc.sta.ssid, CONFIG_DSBOT_WIFI_SSID, sizeof(wc.sta.ssid));
    strlcpy((char *)wc.sta.password, CONFIG_DSBOT_WIFI_PASSWORD, sizeof(wc.sta.password));
    wc.sta.threshold.authmode = WIFI_AUTH_WPA2_PSK;

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wc));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "connecting to WiFi \"%s\" …", CONFIG_DSBOT_WIFI_SSID);
    EventBits_t bits = xEventGroupWaitBits(s_wifi_eg,
                                           WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                                           pdFALSE, pdFALSE, portMAX_DELAY);
    return (bits & WIFI_CONNECTED_BIT) ? ESP_OK : ESP_FAIL;
}

/* ------------------------------------------------------------- WebSocket */
static void ws_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_websocket_event_data_t *d = (esp_websocket_event_data_t *)data;
    switch (id) {
    case WEBSOCKET_EVENT_CONNECTED:
        ESP_LOGI(TAG, "websocket connected");
        s_ws_up = true;
        {
            char hello[320];   /* hello JSON 约 200 字节，别截断 */
            snprintf(hello, sizeof(hello),
                     "{\"v\":\"1\",\"t\":\"hello\",\"token\":\"%s\",\"device_id\":\"ds-bot\","
                     "\"fw\":\"0.1.0\",\"audio\":{\"codec\":\"pcm_s16le\",\"rate\":16000,"
                     "\"channels\":1,\"bits\":16}}",
                     CONFIG_DSBOT_BRIDGE_TOKEN);
            esp_websocket_client_send_text(s_ws, hello, strlen(hello), pdMS_TO_TICKS(2000));
        }
        break;
    case WEBSOCKET_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "websocket disconnected");
        s_ws_up = false;
        if (s_tts_buf) { free(s_tts_buf); s_tts_buf = NULL; s_tts_need = s_tts_got = 0; }
        break;
    case WEBSOCKET_EVENT_DATA:
        if (d->op_code == 0x08) {           /* close */
            break;
        }
        if (d->op_code == 0x02) {           /* binary: 主机 TTS 音频 */
            if (s_tts_buf && s_tts_got + d->data_len <= s_tts_need) {
                memcpy(s_tts_buf + s_tts_got, d->data_ptr, d->data_len);
                s_tts_got += d->data_len;
                if (s_tts_got >= s_tts_need) {
                    uint8_t *pcm = s_tts_buf;
                    size_t len = s_tts_got;
                    s_tts_buf = NULL; s_tts_need = s_tts_got = 0;
                    ESP_LOGI(TAG, "TTS 音频完整: %u 字节", (unsigned)len);
                    if (s_tts_cb) {
                        s_tts_cb(pcm, len);      /* 回调负责 free */
                    } else {
                        free(pcm);
                    }
                }
            }
            break;
        }
        if (d->op_code == 0x01 && d->data_len > 0) {   /* text */
            /* 只在完整帧（未分片）时回调 */
            if (d->payload_offset == 0 && d->payload_len == d->data_len) {
                char *buf = malloc(d->data_len + 1);
                if (buf) {
                    memcpy(buf, d->data_ptr, d->data_len);
                    buf[d->data_len] = 0;
                    const char *type = "?";
                    cJSON *root = cJSON_Parse(buf);
                    if (root) {
                        cJSON *t = cJSON_GetObjectItem(root, "t");
                        if (cJSON_IsString(t)) {
                            type = t->valuestring;
                        }
                        /* speak: 预留接收缓冲，等二进制帧填满后播放 */
                        if (!strcmp(type, "speak")) {
                            cJSON *bytes = cJSON_GetObjectItem(root, "bytes");
                            if (cJSON_IsNumber(bytes) && bytes->valueint > 0) {
                                if (s_tts_buf) { free(s_tts_buf); }
                                s_tts_need = (size_t)bytes->valueint;
                                s_tts_got = 0;
                                s_tts_buf = heap_caps_malloc(s_tts_need, MALLOC_CAP_SPIRAM);
                                ESP_LOGI(TAG, "准备接收 TTS 音频 %u 字节", (unsigned)s_tts_need);
                            }
                        }
                    }
                    ESP_LOGI(TAG, "rx: %s", buf);
                    if (s_msg_cb) {
                        s_msg_cb(type, buf);
                    }
                    if (root) {
                        cJSON_Delete(root);
                    }
                    free(buf);
                }
            }
        }
        break;
    default:
        break;
    }
}

static void ws_start(void)
{
    char uri[96];
    snprintf(uri, sizeof(uri), "ws://%s:%d/ws",
             CONFIG_DSBOT_BRIDGE_HOST, CONFIG_DSBOT_BRIDGE_PORT);
    esp_websocket_client_config_t cfg = {
        .uri = uri,
        .reconnect_timeout_ms = 3000,
        .network_timeout_ms = 8000,
        .buffer_size = 4096,
    };
    ESP_LOGI(TAG, "connecting %s", uri);
    s_ws = esp_websocket_client_init(&cfg);
    esp_websocket_register_events(s_ws, WEBSOCKET_EVENT_ANY, ws_event_handler, NULL);
    esp_websocket_client_start(s_ws);
}

esp_err_t net_start(net_msg_cb_t cb)
{
    s_msg_cb = cb;
    esp_err_t err = wifi_start();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "WiFi 连接失败（SSID 配了吗？见 idf.py menuconfig → ds-bot 配置）");
        return err;
    }
    ws_start();
    return ESP_OK;
}

bool net_connected(void)
{
    return s_ws_up;
}

esp_err_t net_send_json(const char *json)
{
    if (!s_ws || !s_ws_up) {
        return ESP_ERR_INVALID_STATE;
    }
    int n = esp_websocket_client_send_text(s_ws, json, strlen(json), pdMS_TO_TICKS(3000));
    return n < 0 ? ESP_FAIL : ESP_OK;
}

esp_err_t net_send_binary(const void *data, size_t len)
{
    if (!s_ws || !s_ws_up) {
        return ESP_ERR_INVALID_STATE;
    }
    int n = esp_websocket_client_send_bin(s_ws, (const char *)data, len, pdMS_TO_TICKS(3000));
    return n < 0 ? ESP_FAIL : ESP_OK;
}
