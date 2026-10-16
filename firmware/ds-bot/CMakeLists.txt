# ds-bot — Mini Agent Assistant 的 ESP32 终端界面
# 基于 Waveshare ESP32-S3-Touch-AMOLED-2.16 BSP + LVGL 9
cmake_minimum_required(VERSION 3.16)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)

add_compile_options(-Wno-format)
project(ds_bot)
