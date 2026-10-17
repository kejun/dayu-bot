#include "bluebot_player.h"
#include <stddef.h>

typedef struct { uint8_t frame; uint16_t duration_ms; } step_t;
typedef struct { const step_t * steps; uint8_t count; } clip_t;

#define BLINK_OPEN_MS 2800
#define BLINK_CLOSE_MS 120
#define BLINK_HOLD_MS 90
#define BLINK_REOPEN_MS 150
#define BLINK_MOTION_MS (BLINK_CLOSE_MS + BLINK_HOLD_MS + BLINK_REOPEN_MS)
#define BLINK_EYE_WIDTH 28
#define BLINK_OPEN_HEIGHT 28
#define BLINK_CLOSED_HEIGHT 10
#define BLINK_MASK_SIZE 34

static const step_t idle[] = {
    { BLUEBOT_FRAME_IDLE_01, BLINK_OPEN_MS }, { BLUEBOT_FRAME_IDLE_02, BLINK_MOTION_MS }
};
static const step_t listen[] = {
    { BLUEBOT_FRAME_LISTENING_01, 300 }, { BLUEBOT_FRAME_LISTENING_02, 450 },
    { BLUEBOT_FRAME_LISTENING_03, 120 }, { BLUEBOT_FRAME_LISTENING_04, 450 },
    { BLUEBOT_FRAME_LISTENING_05, 120 }, { BLUEBOT_FRAME_LISTENING_01, 300 }
};
static const step_t think[] = {
    { BLUEBOT_FRAME_THINKING_01, 300 }, { BLUEBOT_FRAME_THINKING_02, 300 },
    { BLUEBOT_FRAME_THINKING_03, 450 }
};
static const step_t speak[] = {
    { BLUEBOT_FRAME_SPEAKING_01, 170 }, { BLUEBOT_FRAME_SPEAKING_02, 170 }
};
static const step_t happy[] = {
    { BLUEBOT_FRAME_HAPPY_01, 500 }, { BLUEBOT_FRAME_HAPPY_02, 500 }
};
static const step_t sad[] = {
    { BLUEBOT_FRAME_SAD_01, 350 }, { BLUEBOT_FRAME_SAD_02, 350 },
    { BLUEBOT_FRAME_SAD_03, 700 }, { BLUEBOT_FRAME_SAD_04, 700 },
    { BLUEBOT_FRAME_SAD_03, 700 }, { BLUEBOT_FRAME_SAD_02, 350 }
};

#define CLIP(x) { x, (uint8_t)(sizeof(x) / sizeof((x)[0])) }
static const clip_t clips[BLUEBOT_STATE_COUNT] = {
    CLIP(idle), CLIP(listen), CLIP(think), CLIP(speak), CLIP(happy), CLIP(sad)
};
#undef CLIP

static void blink_set_visible(lv_obj_t * obj, bool visible)
{
#if LVGL_VERSION_MAJOR == 9 && LVGL_VERSION_MINOR >= 6
    lv_obj_set_hidden(obj, !visible);
#else
    if(!visible) lv_obj_add_flag(obj, LV_OBJ_FLAG_HIDDEN);
#if LVGL_VERSION_MAJOR == 9
    else lv_obj_remove_flag(obj, LV_OBJ_FLAG_HIDDEN);
#else
    else lv_obj_clear_flag(obj, LV_OBJ_FLAG_HIDDEN);
#endif
#endif
}

/* The pack's idle frames are identical. Draw square eyes over them in idle,
 * squashing just their height during a blink; no new image/framebuffer. */
static void blink_create(bluebot_player_t * player)
{
    lv_color_t background = lv_obj_get_style_bg_color(lv_obj_get_parent(player->image), LV_PART_MAIN);
    for(unsigned i = 0; i < 2; ++i) {
        lv_obj_t * mask = lv_obj_create(player->image);
        lv_obj_remove_style_all(mask);
        lv_obj_set_size(mask, BLINK_MASK_SIZE, BLINK_MASK_SIZE);
        lv_obj_set_pos(mask, i ? 137 : 84, 126);
        lv_obj_set_style_bg_color(mask, background, 0);
        lv_obj_set_style_bg_opa(mask, LV_OPA_COVER, 0);
        blink_set_visible(mask, false);
        player->blink_masks[i] = mask;

        lv_obj_t * lid = lv_obj_create(mask);
        lv_obj_remove_style_all(lid);
        lv_obj_set_size(lid, BLINK_EYE_WIDTH, BLINK_OPEN_HEIGHT);
        lv_obj_set_pos(lid, 3, 3);
        lv_obj_set_style_bg_color(lid, lv_color_hex(0x000C29), 0);
        lv_obj_set_style_bg_opa(lid, LV_OPA_COVER, 0);
        lv_obj_set_style_radius(lid, 2, 0);
        player->blink_lids[i] = lid;
    }
    player->blink_height = BLINK_OPEN_HEIGHT;
    player->blink_visible = false;
}

static void blink_tick(bluebot_player_t * player, uint32_t now_ms)
{
    bool visible = player->state == BLUEBOT_IDLE;
    if(player->blink_visible != visible) {
        for(unsigned i = 0; i < 2; ++i) blink_set_visible(player->blink_masks[i], visible);
        player->blink_visible = visible;
    }
    if(!visible) return;
    uint32_t phase = (uint32_t)(now_ms - player->started_at) % (BLINK_OPEN_MS + BLINK_MOTION_MS);
    uint32_t height = BLINK_OPEN_HEIGHT;
    if(phase >= BLINK_OPEN_MS) {
        uint32_t t = phase - BLINK_OPEN_MS;
        uint32_t travel = BLINK_OPEN_HEIGHT - BLINK_CLOSED_HEIGHT;
        if(t < BLINK_CLOSE_MS) height -= travel * t / BLINK_CLOSE_MS;
        else if(t < BLINK_CLOSE_MS + BLINK_HOLD_MS) height = BLINK_CLOSED_HEIGHT;
        else height = BLINK_CLOSED_HEIGHT + travel * (t - BLINK_CLOSE_MS - BLINK_HOLD_MS) / BLINK_REOPEN_MS;
    }
    if(height == player->blink_height) return;
    for(unsigned i = 0; i < 2; ++i) {
        lv_obj_set_size(player->blink_lids[i], BLINK_EYE_WIDTH, height);
        lv_obj_set_pos(player->blink_lids[i], 3, (BLINK_MASK_SIZE - height) / 2);
    }
    player->blink_height = (uint8_t)height;
}

void bluebot_player_tick(bluebot_player_t * player, uint32_t now_ms)
{
    if(!player || !player->image || (unsigned)player->state >= BLUEBOT_STATE_COUNT) return;
    blink_tick(player, now_ms);
    const clip_t * clip = &clips[player->state];
    uint32_t cycle_ms = 0;
    for(uint8_t i = 0; i < clip->count; ++i) cycle_ms += clip->steps[i].duration_ms;
    uint32_t phase_ms = (uint32_t)(now_ms - player->started_at) % cycle_ms;
    uint8_t frame = clip->steps[0].frame;
    for(uint8_t i = 0; i < clip->count; ++i) {
        frame = clip->steps[i].frame;
        if(phase_ms < clip->steps[i].duration_ms) break;
        phase_ms -= clip->steps[i].duration_ms;
    }
    if(frame == player->shown_frame) return;
#if LVGL_VERSION_MAJOR == 9
    lv_image_set_src(player->image, bluebot_frames[frame]);
#else
    lv_img_set_src(player->image, bluebot_frames[frame]);
#endif
    player->shown_frame = frame;
}

bool bluebot_player_bind(bluebot_player_t * player, lv_obj_t * image, uint32_t now_ms)
{
    if(!player || !image) return false;
    player->image = image;
    player->started_at = now_ms;
    player->state = BLUEBOT_IDLE;
    player->shown_frame = BLUEBOT_FRAME_COUNT;
    blink_create(player);
    bluebot_player_tick(player, now_ms);
    return true;
}

bool bluebot_player_set_state(bluebot_player_t * player, bluebot_state_t state, uint32_t now_ms)
{
    if(!player || !player->image || (unsigned)state >= BLUEBOT_STATE_COUNT) return false;
    if(player->state != state) {
        player->state = state;
        player->started_at = now_ms;
        bluebot_player_tick(player, now_ms);
    }
    return true;
}

void bluebot_player_unbind(bluebot_player_t * player)
{
    if(!player || !player->image) return;
    for(unsigned i = 0; i < 2; ++i) {
#if LVGL_VERSION_MAJOR == 9
        lv_obj_delete(player->blink_masks[i]);
#else
        lv_obj_del(player->blink_masks[i]);
#endif
        player->blink_masks[i] = NULL;
        player->blink_lids[i] = NULL;
    }
    player->image = NULL;
}
