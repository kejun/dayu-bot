idf_component_register(
    SRCS "main.c" "audio.c" "buttons.c" "net.c"
    INCLUDE_DIRS "."
    REQUIRES esp32_s3_touch_amoled_2_16 lvgl uface esp_codec_dev esp_driver_gpio
             esp_wifi esp_netif esp_event json esp_websocket_client nvs_flash
)
