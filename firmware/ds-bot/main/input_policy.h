#pragma once

#include <stdbool.h>
#include <stdint.h>

#define INPUT_CANCEL_HOLD_MS 2000u
typedef enum { INPUT_NONE, INPUT_RECORD_START, INPUT_RECORD_STOP, INPUT_CANCEL } input_action_t;
typedef struct {
    bool down;
    bool cancel_candidate;
    bool cancel_sent;
    uint32_t pressed_ms;
    uint32_t task_epoch;
} input_key_t;
typedef struct {
    input_key_t keys[2];
    int recording_key; /* -1 表示没有录音键拥有本次按住录音 */
} input_policy_t;

void input_policy_init(input_policy_t *policy);
input_action_t input_policy_edge(input_policy_t *policy, int key, bool pressed,
                                 bool task_active, uint32_t task_epoch,
                                 bool recording_available, uint32_t now_ms);
input_action_t input_policy_poll(input_policy_t *policy, bool task_active,
                                 uint32_t task_epoch, uint32_t now_ms);
