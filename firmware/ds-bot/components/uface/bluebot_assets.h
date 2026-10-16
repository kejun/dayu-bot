/* Generated PNG assets. Compile exactly one size/version directory. */
#ifndef BLUEBOT_ASSETS_H
#define BLUEBOT_ASSETS_H
#include "lvgl.h"
#ifdef __cplusplus
extern "C" {
#endif

#if LVGL_VERSION_MAJOR != 9
#error "This asset directory targets LVGL 9. Choose the matching directory."
#endif
#if LVGL_VERSION_MINOR < 2
#error "This package targets LVGL 9.2 headers and is verified with 9.2.2. Earlier 9.x is not supported."
#endif

#if defined(LV_BIG_ENDIAN_SYSTEM) && LV_BIG_ENDIAN_SYSTEM
#error "These assets target little-endian MCUs such as ESP32."
#endif
#define BLUEBOT_WIDTH 128
#define BLUEBOT_HEIGHT 128
typedef lv_image_dsc_t bluebot_image_dsc_t;

typedef enum {
    BLUEBOT_FRAME_IDLE_01 = 0,
    BLUEBOT_FRAME_IDLE_02 = 1,
    BLUEBOT_FRAME_LISTENING_01 = 2,
    BLUEBOT_FRAME_LISTENING_02 = 3,
    BLUEBOT_FRAME_LISTENING_03 = 4,
    BLUEBOT_FRAME_LISTENING_04 = 5,
    BLUEBOT_FRAME_LISTENING_05 = 6,
    BLUEBOT_FRAME_THINKING_01 = 7,
    BLUEBOT_FRAME_THINKING_02 = 8,
    BLUEBOT_FRAME_THINKING_03 = 9,
    BLUEBOT_FRAME_SPEAKING_01 = 10,
    BLUEBOT_FRAME_SPEAKING_02 = 11,
    BLUEBOT_FRAME_HAPPY_01 = 12,
    BLUEBOT_FRAME_HAPPY_02 = 13,
    BLUEBOT_FRAME_SAD_01 = 14,
    BLUEBOT_FRAME_SAD_02 = 15,
    BLUEBOT_FRAME_SAD_03 = 16,
    BLUEBOT_FRAME_SAD_04 = 17,
    BLUEBOT_FRAME_COUNT = 18
} bluebot_frame_t;

extern const bluebot_image_dsc_t bluebot_idle_01;
extern const bluebot_image_dsc_t bluebot_idle_02;
extern const bluebot_image_dsc_t bluebot_listening_01;
extern const bluebot_image_dsc_t bluebot_listening_02;
extern const bluebot_image_dsc_t bluebot_listening_03;
extern const bluebot_image_dsc_t bluebot_listening_04;
extern const bluebot_image_dsc_t bluebot_listening_05;
extern const bluebot_image_dsc_t bluebot_thinking_01;
extern const bluebot_image_dsc_t bluebot_thinking_02;
extern const bluebot_image_dsc_t bluebot_thinking_03;
extern const bluebot_image_dsc_t bluebot_speaking_01;
extern const bluebot_image_dsc_t bluebot_speaking_02;
extern const bluebot_image_dsc_t bluebot_happy_01;
extern const bluebot_image_dsc_t bluebot_happy_02;
extern const bluebot_image_dsc_t bluebot_sad_01;
extern const bluebot_image_dsc_t bluebot_sad_02;
extern const bluebot_image_dsc_t bluebot_sad_03;
extern const bluebot_image_dsc_t bluebot_sad_04;
extern const bluebot_image_dsc_t * const bluebot_frames[BLUEBOT_FRAME_COUNT];

#ifdef __cplusplus
}
#endif
#endif
