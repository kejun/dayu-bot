idf_component_register(
    SRCS "main.c" "audio.c" "buttons.c"
    INCLUDE_DIRS "."
    REQUIRES esp32_s3_touch_amoled_2_16 lvgl uface esp_codec_dev esp_driver_gpio
)
