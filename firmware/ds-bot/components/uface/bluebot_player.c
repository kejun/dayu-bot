#include "bluebot_player.h"
#include <stddef.h>

typedef struct { uint8_t frame; uint16_t duration_ms; } step_t;
typedef struct { const step_t * steps; uint8_t count; } clip_t;

static const step_t idle[] = {
    { BLUEBOT_FRAME_IDLE_01, 2800 }, { BLUEBOT_FRAME_IDLE_02, 150 }
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

void bluebot_player_tick(bluebot_player_t * player, uint32_t now_ms)
{
    if(!player || !player->image || (unsigned)player->state >= BLUEBOT_STATE_COUNT) return;
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
    if(player) player->image = NULL;
}
