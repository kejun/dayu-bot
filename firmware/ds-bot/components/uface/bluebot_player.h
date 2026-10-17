#ifndef BLUEBOT_PLAYER_H
#define BLUEBOT_PLAYER_H

#include <stdbool.h>
#include <stdint.h>
#include "bluebot_assets.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    BLUEBOT_IDLE,
    BLUEBOT_LISTEN,
    BLUEBOT_THINK,
    BLUEBOT_SPEAK,
    BLUEBOT_HAPPY,
    BLUEBOT_SAD,
    BLUEBOT_STATE_COUNT
} bluebot_state_t;

/* Caller owns this struct and image. Blink masks are image children; no timers/framebuffers. */
typedef struct {
    lv_obj_t * image;
    lv_obj_t * blink_masks[2];
    lv_obj_t * blink_lids[2];
    uint32_t started_at;
    bluebot_state_t state;
    uint8_t shown_frame;
    uint8_t blink_height;
    bool blink_visible;
} bluebot_player_t;

/* Pass an existing lv_img (v8) or lv_image (v9) widget. */
bool bluebot_player_bind(bluebot_player_t * player, lv_obj_t * image, uint32_t now_ms);
/* Repeating the current state does not restart the animation. */
bool bluebot_player_set_state(bluebot_player_t * player, bluebot_state_t state, uint32_t now_ms);
/* Call about every 20-50 ms in the LVGL thread, or while holding its lock. */
void bluebot_player_tick(bluebot_player_t * player, uint32_t now_ms);
/* Delete the blink masks and unbind before deleting the image. */
void bluebot_player_unbind(bluebot_player_t * player);

#ifdef __cplusplus
}
#endif
#endif
