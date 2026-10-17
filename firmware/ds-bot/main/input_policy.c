#include "input_policy.h"
#include <string.h>

void input_policy_init(input_policy_t *policy)
{
    memset(policy, 0, sizeof(*policy));
    policy->recording_key = -1;
}

input_action_t input_policy_edge(input_policy_t *policy, int key, bool pressed,
                                 bool task_active, uint32_t task_epoch,
                                 bool recording_available, uint32_t now_ms)
{
    if (key < 0 || key >= 2) return INPUT_NONE;
    input_key_t *k = &policy->keys[key];
    if (k->down == pressed) return INPUT_NONE;
    k->down = pressed;
    if (pressed) {
        k->cancel_candidate = task_active;
        k->cancel_sent = false;
        k->task_epoch = task_epoch;
        k->pressed_ms = now_ms;
        if (!task_active && recording_available && policy->recording_key < 0) {
            policy->recording_key = key;
            return INPUT_RECORD_START;
        }
    } else {
        k->cancel_candidate = false;
        if (policy->recording_key == key) {
            policy->recording_key = -1;
            return INPUT_RECORD_STOP;
        }
    }
    return INPUT_NONE;
}

input_action_t input_policy_poll(input_policy_t *policy, bool task_active,
                                 uint32_t task_epoch, uint32_t now_ms)
{
    if (!task_active) return INPUT_NONE;
    for (int i = 0; i < 2; ++i) {
        input_key_t *k = &policy->keys[i];
        if (k->down && k->cancel_candidate && !k->cancel_sent &&
            k->task_epoch == task_epoch && now_ms - k->pressed_ms >= INPUT_CANCEL_HOLD_MS) {
            /* 交叉按下两个键也只为同一轮任务发送一次。 */
            for (int j = 0; j < 2; ++j) policy->keys[j].cancel_sent = true;
            return INPUT_CANCEL;
        }
    }
    return INPUT_NONE;
}
