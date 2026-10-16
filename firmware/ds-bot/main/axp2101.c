#include "axp2101.h"

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "bsp/esp-bsp.h"

static const char *TAG = "ds-bot-axp";

#define REG_INTEN2   0x41
#define REG_INTSTS2  0x49
#define BIT_PKEY_SHORT  (1 << 3)

static i2c_master_dev_handle_t s_dev;
static axp_pwrkey_cb_t s_cb;

static esp_err_t reg_read(uint8_t reg, uint8_t *val)
{
    return i2c_master_transmit_receive(s_dev, &reg, 1, val, 1, 100);
}

static esp_err_t reg_write(uint8_t reg, uint8_t val)
{
    uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_dev, buf, sizeof(buf), 100);
}

esp_err_t axp2101_chip_id(uint8_t *id)
{
    if (!s_dev) {
        return ESP_ERR_INVALID_STATE;
    }
    return reg_read(0x03, id);
}

static void axp_task(void *arg)
{
    (void)arg;
    while (1) {
        uint8_t sts = 0;
        if (reg_read(REG_INTSTS2, &sts) == ESP_OK && (sts & BIT_PKEY_SHORT)) {
            reg_write(REG_INTSTS2, BIT_PKEY_SHORT);   /* 写 1 清除 */
            ESP_LOGI(TAG, "PWRKEY short press");
            if (s_cb) {
                s_cb();
            }
        }
        vTaskDelay(pdMS_TO_TICKS(50));
    }
}

esp_err_t axp2101_init(axp_pwrkey_cb_t cb)
{
    s_cb = cb;

    /* 复用 BSP 已经建好的 I2C 总线（触摸/音频 codec 也在上面） */
    i2c_master_bus_handle_t bus = bsp_i2c_get_handle();
    if (!bus) {
        ESP_LOGE(TAG, "BSP I2C 总线不可用");
        return ESP_ERR_INVALID_STATE;
    }

    i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = AXP2101_ADDR,
        .scl_speed_hz = 400000,
    };
    esp_err_t err = i2c_master_bus_add_device(bus, &dev_cfg, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "添加 AXP2101 设备失败: %s", esp_err_to_name(err));
        return err;
    }

    uint8_t id = 0;
    if (reg_read(0x03, &id) == ESP_OK) {
        ESP_LOGI(TAG, "AXP2101 chip id = 0x%02X", id);
    } else {
        ESP_LOGW(TAG, "读芯片 ID 失败（I2C 上可能没有 AXP2101）");
    }

    /* 先清掉残留的中断标志（开机那一下按键会留下 PWRKEY 标志，否则一上电就误触发） */
    reg_write(0x48, 0xFF);
    reg_write(0x49, 0xFF);
    reg_write(0x4A, 0xFF);

    /* 使能 PWRKEY 短按中断 */
    uint8_t inten = 0;
    if (reg_read(REG_INTEN2, &inten) == ESP_OK) {
        err = reg_write(REG_INTEN2, inten | BIT_PKEY_SHORT);
        ESP_LOGI(TAG, "INTEN2 0x%02X -> 0x%02X", inten, inten | BIT_PKEY_SHORT);
    } else {
        err = ESP_FAIL;
    }
    if (err != ESP_OK) {
        return err;
    }

    /* 清完标志再等一会儿，避开开机那一下 */
    vTaskDelay(pdMS_TO_TICKS(300));

    if (xTaskCreate(axp_task, "dsbot_axp", 3072, NULL, 4, NULL) != pdPASS) {
        return ESP_ERR_NO_MEM;
    }
    ESP_LOGI(TAG, "PWRKEY 短按检测已启动（中间键）");
    return ESP_OK;
}
