#include "buttons.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/gpio.h"
#include "esp_log.h"

static const char *TAG = "ds-bot-btn";

static const int k_pins[] = {BTN_GPIO_KEY3, BTN_GPIO_KEY2};
#define N_PINS (sizeof(k_pins) / sizeof(k_pins[0]))

static btn_event_cb_t s_cb;
static bool s_last[N_PINS];
static int  s_stable[N_PINS];    /* 连续同电平的采样次数，用于去抖 */

#define POLL_MS   10
#define DEBOUNCE  3              /* 30ms */

static void btn_task(void *arg)
{
    (void)arg;
    while (1) {
        for (size_t i = 0; i < N_PINS; i++) {
            int level = gpio_get_level(k_pins[i]);      /* 0 = 按下 */
            int want = level ? 0 : 1;                    /* 1 = 按下 */
            if (want == s_last[i]) {
                s_stable[i] = 0;
            } else if (++s_stable[i] >= DEBOUNCE) {
                s_last[i] = want;
                s_stable[i] = 0;
                ESP_LOGI(TAG, "GPIO%d %s", k_pins[i], want ? "pressed" : "released");
                if (s_cb) {
                    s_cb(k_pins[i], want);
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

esp_err_t buttons_init(btn_event_cb_t cb)
{
    s_cb = cb;

    uint64_t mask = 0;
    for (size_t i = 0; i < N_PINS; i++) {
        mask |= (1ULL << k_pins[i]);
    }
    gpio_config_t cfg = {
        .pin_bit_mask = mask,
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,      /* 板上有 10K 外部上拉，这里再开内部上拉更稳 */
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    ESP_ERROR_CHECK(gpio_config(&cfg));

    for (size_t i = 0; i < N_PINS; i++) {
        s_last[i] = gpio_get_level(k_pins[i]) ? 0 : 1;
    }

    if (xTaskCreate(btn_task, "dsbot_btn", 3072, NULL, 5, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "buttons ready: GPIO%d(Key3), GPIO%d(Key2/BOOT); middle key is PWRON (AXP2101)",
             BTN_GPIO_KEY3, BTN_GPIO_KEY2);
    return ESP_OK;
}
